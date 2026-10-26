"""One DSH-backed text chat generation service for chat and debug.

Identity, admission, reservation, deduplication, notification, fulfillment and
post-delivery promises stay in Python. This module prepares bounded data,
performs exactly one automatic recall, runs one DSH task, and validates the
returned business result against the private Python candidate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from nonebot import logger

from komari_bot.agent import (
    AgentHost,
    AgentTaskBudget,
    AgentTaskRequest,
    TaskKind,
)

from .admission_gate import effect_business_admitted
from .chat_candidate import (
    ChatCandidate,
    ChatResultMismatchError,
    ChatSubmissionReceipt,
)
from .chat_recall_query import RecallQueryBudgetError, build_automatic_recall_query
from .chat_result import ReplyResult
from .chat_task_data import build_chat_task_data

if TYPE_CHECKING:
    from komari_bot.agent import AgentCallbackRequest, JsonValue
    from komari_bot.context.redis_store import MessageSchema
    from komari_bot.plugins.komari_memory import HindsightRecallSession

    from .reply_context import ReplyContext

class ChatConfigLike(Protocol):
    """Trusted config facts read once at task start."""

    bot_nickname: str
    knowledge_enabled: bool
    knowledge_limit: int
    agent_max_rounds: int
    agent_max_tool_calls_per_round: int
    agent_max_total_tool_calls: int


class UserDataConfigLike(Protocol):
    """Trusted user-data cap read once at task start."""

    max_favorability_delta_per_reply: int


KnowledgeSearch = Callable[..., Awaitable[Sequence[object]]]
UidSearch = Callable[[str], Awaitable[Sequence[object]]]
FavorabilityGetter = Callable[[str], Awaitable[object]]
GroupBindingsGetter = Callable[[str], Mapping[str, str]]
HostGetter = Callable[[], AgentHost]

MAX_DSH_BUDGET_VALUE = 1_000


class DshChatError(RuntimeError):
    """Base fixed-code failure at the DSH chat boundary."""

    code = "dsh_chat_failed"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class ChatTaskBudgetError(DshChatError):
    """The configured three-value chat budget cannot be frozen."""

    code = "invalid_chat_budget"


class ChatFavorabilityReadError(DshChatError):
    """Reading the task's current favorability failed."""

    code = "favorability_read_failed"


class ImageTransportUnavailableError(DshChatError):
    """A text-only DSH chat task reached an image seam owned by TSK-205."""

    code = "image_transport_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ChatTaskBudget:
    """The three existing chat budget values frozen at task start."""

    rounds: int
    per_round: int
    total: int

    @classmethod
    def from_config(cls, config: object) -> ChatTaskBudget:
        chat_config = cast("ChatConfigLike", config)
        try:
            values = (
                chat_config.agent_max_rounds,
                chat_config.agent_max_tool_calls_per_round,
                chat_config.agent_max_total_tool_calls,
            )
        except AttributeError:
            raise ChatTaskBudgetError from None
        if not all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= MAX_DSH_BUDGET_VALUE
            for value in values
        ):
            raise ChatTaskBudgetError
        return cls(rounds=values[0], per_round=values[1], total=values[2])

    def to_agent_budget(self) -> AgentTaskBudget:
        return AgentTaskBudget(
            max_rounds=self.rounds,
            max_tool_calls_per_round=self.per_round,
            max_total_tool_calls=self.total,
        )


class DshChatService:
    """Task-scoped generation through the production shared ``AgentHost``."""

    def __init__(
        self,
        *,
        host_getter: HostGetter | None = None,
        knowledge_search: KnowledgeSearch | None = None,
        uid_search: UidSearch | None = None,
        favorability_getter: FavorabilityGetter | None = None,
        group_bindings_getter: GroupBindingsGetter | None = None,
    ) -> None:
        self._host_getter = host_getter or _default_host_getter
        self._knowledge_search = knowledge_search or _default_knowledge_search
        self._uid_search = uid_search or _default_uid_search
        self._favorability_getter = favorability_getter or _default_favorability
        self._group_bindings_getter = group_bindings_getter or _default_group_bindings

    async def generate(
        self,
        *,
        message: MessageSchema,
        recent_messages: Sequence[MessageSchema],
        reply_context: ReplyContext | None,
        recall_session: HindsightRecallSession,
        request_trace_id: str,
        config: object,
        caller_is_superuser: bool = False,
        image_urls: Sequence[str] | None = None,
    ) -> ReplyResult:
        """Generate one text chat candidate and verify the host result."""
        del caller_is_superuser
        if not _task_id_is_valid(request_trace_id):
            raise DshChatError("invalid_trace_id") from None
        if not effect_business_admitted(group_id=message.group_id):
            return ReplyResult(content="")

        chat_config = cast("ChatConfigLike", config)
        budget = ChatTaskBudget.from_config(config)
        recall_query = build_automatic_recall_query(
            current=message,
            recent=recent_messages,
            quote=reply_context,
        )

        # Re-evaluate admission immediately before the automatic recall effect.
        if not effect_business_admitted(group_id=message.group_id):
            return ReplyResult(content="")

        # TSK-203 does not skip automatic recall for an image-only trigger. The
        # unmigrated image seam fails closed immediately after that single
        # read-only recall and before any DSH generation or knowledge lookup.
        recall_response = await recall_session.recall(recall_query)
        if image_urls or (
            reply_context is not None and reply_context.image_sources
        ):
            raise ImageTransportUnavailableError

        favorability = await self._load_favorability(message.user_id)
        try:
            raw_cap = cast("UserDataConfigLike", _user_data_config()).max_favorability_delta_per_reply
        except Exception:
            raise ChatFavorabilityReadError from None
        if (
            isinstance(raw_cap, bool)
            or not isinstance(raw_cap, int)
            or raw_cap < 0
        ):
            raise ChatFavorabilityReadError from None
        max_favorability_delta = raw_cap

        knowledge_results, uid_knowledge_results = await self._load_knowledge(
            message=message,
            recent_messages=recent_messages,
            reply_context=reply_context,
            config=chat_config,
        )
        group_bindings = self._load_group_bindings(message.group_id)
        task_data = build_chat_task_data(
            current=message,
            recent=recent_messages,
            quote=reply_context,
            memory_results=list(getattr(recall_response, "results", None) or []),
            knowledge_results=knowledge_results,
            uid_knowledge_results=uid_knowledge_results,
            favorability=favorability,
            bot_nickname=str(chat_config.bot_nickname),
            max_favorability_delta=max_favorability_delta,
            group_bindings=group_bindings,
        )

        candidate = ChatCandidate(max_favorability_delta=max_favorability_delta)

        async def admission(_request: AgentCallbackRequest) -> JsonValue:
            try:
                allow = effect_business_admitted(group_id=message.group_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                allow = False
            return {"allow": bool(allow)}

        async def submit_final_response(
            request: AgentCallbackRequest,
        ) -> JsonValue:
            return _receipt_payload(candidate.submit_final_response(request.data))

        async def submit_favorability(request: AgentCallbackRequest) -> JsonValue:
            return _receipt_payload(candidate.submit_favorability(request.data))

        host = self._host_getter()
        task = AgentTaskRequest(
            task_id=request_trace_id,
            kind=TaskKind.CHAT,
            task_data=cast("dict[str, JsonValue]", task_data.to_dict()),
            budget=budget.to_agent_budget(),
        )
        result = await host.execute(
            task,
            {
                "admission": admission,
                "chat.submit_final_response": submit_final_response,
                "chat.submit_favorability": submit_favorability,
            },
        )
        content, delta, reason = candidate.verify_host_result(result.result)
        logger.info(
            "[KomariChat] DSH chat generated: trace_id={} recent={} memory={} knowledge={} uid_knowledge={} content_chars={}",
            request_trace_id,
            len(task_data.recent),
            len(task_data.memory),
            len(task_data.knowledge),
            len(task_data.uid_knowledge),
            len(content),
        )
        return ReplyResult(
            content=content,
            favorability_delta=delta,
            favorability_reason=reason,
        )

    async def _load_favorability(self, user_id: str) -> object:
        try:
            return await self._favorability_getter(user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ChatFavorabilityReadError from None

    async def _load_knowledge(
        self,
        *,
        message: MessageSchema,
        recent_messages: Sequence[MessageSchema],
        reply_context: ReplyContext | None,
        config: ChatConfigLike,
    ) -> tuple[list[object], list[tuple[str, object]]]:
        knowledge_results: list[object] = []
        if bool(config.knowledge_enabled):
            try:
                knowledge_results = list(await self._knowledge_search(
                    query=message.content,
                    limit=int(config.knowledge_limit),
                    embedding_guard=lambda: effect_business_admitted(
                        group_id=message.group_id
                    ),
                    raw_query=True,
                ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "[KomariChat] automatic knowledge lookup failed: error_type={}",
                    type(exc).__name__,
                )

        uid_results: list[tuple[str, object]] = []
        for uid in _uid_candidates(
            current=message,
            recent_messages=recent_messages,
            reply_context=reply_context,
        ):
            try:
                hits = list(await self._uid_search(uid))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "[KomariChat] UID knowledge lookup failed: error_type={}",
                    type(exc).__name__,
                )
                continue
            uid_results.extend((uid, hit) for hit in hits)
        return knowledge_results, uid_results

    def _load_group_bindings(self, group_id: str) -> Mapping[str, str]:
        try:
            return self._group_bindings_getter(group_id)
        except Exception as exc:
            logger.debug(
                "[KomariChat] group binding lookup failed: error_type={}",
                type(exc).__name__,
            )
            return {}


def _receipt_payload(receipt: ChatSubmissionReceipt) -> JsonValue:
    return cast("JsonValue", receipt.to_dict())


def _task_id_is_valid(value: str) -> bool:
    import re

    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is not None
    )


def _uid_candidates(
    *,
    current: MessageSchema,
    recent_messages: Sequence[MessageSchema],
    reply_context: ReplyContext | None,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)

    for message in recent_messages:
        if message.group_id == current.group_id and not message.is_bot:
            add(message.user_id)
    if (
        reply_context is not None
        and reply_context.source_side == "user"
        and reply_context.user_id
    ):
        add(reply_context.user_id)
    add(current.user_id)
    return ordered


def _default_host_getter() -> AgentHost:
    from komari_bot.plugins import komari_agent

    return komari_agent.get_agent_host()


async def _default_knowledge_search(
    query: str,
    limit: int | None = None,
    *,
    embedding_guard: Callable[[], bool] | None = None,
    raw_query: bool = False,
) -> Sequence[object]:
    from komari_bot.plugins import komari_knowledge

    return await komari_knowledge.search_knowledge(
        query,
        limit,
        embedding_guard=embedding_guard,
        raw_query=raw_query,
    )


async def _default_uid_search(uid: str) -> Sequence[object]:
    from komari_bot.plugins import komari_knowledge

    return await komari_knowledge.search_by_keyword(uid)


async def _default_favorability(user_id: str) -> object:
    from komari_bot.plugins import user_data

    return await user_data.get_user_favorability(user_id)


def _default_group_bindings(group_id: str) -> Mapping[str, str]:
    from komari_bot.plugins import character_binding

    manager = character_binding.get_binding_manager()
    return manager.list_onebot_group_bindings(group_id=group_id)


def _user_data_config() -> object:
    from komari_bot.plugins import user_data

    return user_data.get_config()


__all__ = [
    "ChatFavorabilityReadError",
    "ChatResultMismatchError",
    "ChatTaskBudget",
    "ChatTaskBudgetError",
    "DshChatError",
    "DshChatService",
    "ImageTransportUnavailableError",
    "RecallQueryBudgetError",
]
