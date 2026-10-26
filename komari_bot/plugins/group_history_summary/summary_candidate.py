"""TSK-206 总结任务本地的历史选择、可信分页与完成回执。

模型永远看不到群身份、任务预算或完整的 Python 选择；它只通过四个公开工具
触发可信回调。分页回调不是模型工具，只读取冻结的选择代次。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from komari_bot.agent.types import MAX_RESULT_BYTES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .history_service import HistoryFetchMetadata, HistoryMessage
    from .history_tools import SummaryToolResult

MAX_HISTORY_PAGE_BYTES = 192 * 1024
MAX_HISTORY_PAGE_ROWS = 64
MAX_ROW_CONTENT_CHARS = 240

MissingRequirement = Literal["history_read", "final_summary"]
SubmissionCode = Literal["history_required", "invalid_content", "already_completed"]


class SummaryCandidateError(RuntimeError):
    """总结候选状态的固定失败基类。"""

    code = "summary_candidate_invalid"


class SummaryStaleGenerationError(SummaryCandidateError):
    """分页使用了已经失效的选择代次。"""

    code = "stale_generation"


class SummaryPageRangeError(SummaryCandidateError):
    """分页偏移超出冻结选择范围。"""

    code = "invalid_page_offset"


class SummaryPageTooLargeError(SummaryCandidateError):
    """单行内容无法装入可信回调传输信封。"""

    code = "history_page_too_large"


class SummaryCompletedError(SummaryCandidateError):
    """完成后的历史或分页效果必须被拒绝。"""

    code = "already_completed"


class SummaryResultMismatchError(SummaryCandidateError):
    """DSH 返回结果与 Python 暂存的总结候选不一致。"""

    code = "summary_result_mismatch"


@dataclass(frozen=True, slots=True)
class SummarySelection:
    """一份完整、不可变的 Python 历史选择。"""

    generation: int
    source: str
    matched_count: int
    messages: tuple[HistoryMessage, ...]
    filters: Mapping[str, Any]
    history_fetch: HistoryFetchMetadata | None


@dataclass(frozen=True, slots=True)
class SummarySubmissionReceipt:
    """final_summary 的精确结构化回执。"""

    accepted: bool
    code: SubmissionCode | None
    missing: tuple[MissingRequirement, ...]
    result: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "code": self.code,
            "missing": list(self.missing),
            "result": None if self.result is None else dict(self.result),
        }


def _result_fits_envelope(summary_text: str) -> bool:
    encoded = json.dumps(
        {"summary_text": summary_text},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) <= MAX_RESULT_BYTES


def _serialized_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


class SummaryCandidate:
    """One task's private selection generation and staged final summary."""

    __slots__ = ("_bot_self_id", "_completed", "_generation", "_selection", "_summary_text")

    def __init__(self, *, bot_self_id: str) -> None:
        self._bot_self_id = bot_self_id
        self._generation = 0
        self._selection: SummarySelection | None = None
        self._summary_text: str | None = None
        self._completed = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def selection(self) -> SummarySelection | None:
        return self._selection

    @property
    def is_complete(self) -> bool:
        return self._completed

    @property
    def missing_requirements(self) -> tuple[MissingRequirement, ...]:
        if self._completed:
            return ()
        if self._selection is None:
            return ("history_read", "final_summary")
        return ("final_summary",)

    def replace_selection(self, result: SummaryToolResult) -> int:
        """Replace the task-local selection after one successful public read."""
        if self._completed:
            raise SummaryCompletedError
        self._generation += 1
        self._selection = SummarySelection(
            generation=self._generation,
            source=result.source,
            matched_count=result.matched_count,
            messages=tuple(result.messages),
            filters=dict(result.filters),
            history_fetch=result.history_fetch,
        )
        return self._generation

    def descriptor(self) -> dict[str, Any]:
        """The first-page cursor and source metadata for one frozen selection."""
        selection = self._selection
        if selection is None:
            raise SummaryStaleGenerationError
        return {
            "generation": selection.generation,
            "source": selection.source,
            "matched_count": selection.matched_count,
            "selected_count": len(selection.messages),
            "filters": dict(selection.filters),
            "history_fetch": (
                None
                if selection.history_fetch is None
                else selection.history_fetch.to_public_dict()
            ),
            "first_page_offset": 0,
        }

    def page(self, generation: int, offset: int) -> dict[str, Any]:
        """Read one bounded page from the frozen selection without platform I/O."""
        if self._completed:
            raise SummaryCompletedError
        selection = self._selection
        if selection is None or generation != selection.generation:
            raise SummaryStaleGenerationError
        total = len(selection.messages)
        if offset < 0 or offset > total:
            raise SummaryPageRangeError

        rows: list[dict[str, Any]] = []
        next_offset: int | None = offset
        for message in selection.messages[offset:]:
            if len(rows) >= MAX_HISTORY_PAGE_ROWS:
                break
            row = self._history_row(message, selection.source)
            if _serialized_size(row) > MAX_HISTORY_PAGE_BYTES:
                raise SummaryPageTooLargeError
            rows.append(row)
            next_offset = offset + len(rows)
            if next_offset >= total:
                next_offset = None
            payload = self._page_payload(
                generation=generation,
                offset=offset,
                next_offset=next_offset,
                total=total,
                rows=rows,
            )
            if _serialized_size(payload) > MAX_HISTORY_PAGE_BYTES:
                rows.pop()
                if not rows:
                    raise SummaryPageTooLargeError
                next_offset = offset + len(rows)
                break

        if next_offset is not None and next_offset >= total:
            next_offset = None
        return self._page_payload(
            generation=generation,
            offset=offset,
            next_offset=next_offset,
            total=total,
            rows=rows,
        )

    def submit_final(self, payload: Mapping[str, object]) -> SummarySubmissionReceipt:
        """Validate and synchronously stage the final summary content."""
        if self._completed:
            return SummarySubmissionReceipt(
                accepted=False,
                code="already_completed",
                missing=(),
                result=self.complete_result(),
            )
        if self._selection is None:
            return SummarySubmissionReceipt(
                accepted=False,
                code="history_required",
                missing=self.missing_requirements,
                result=None,
            )

        summary_text: str | None = None
        if set(payload) == {"content"}:
            raw_content = payload.get("content")
            if isinstance(raw_content, str):
                stripped = raw_content.strip()
                if stripped and _result_fits_envelope(stripped):
                    summary_text = stripped
        if summary_text is None:
            return SummarySubmissionReceipt(
                accepted=False,
                code="invalid_content",
                missing=self.missing_requirements,
                result=None,
            )

        self._summary_text = summary_text
        self._completed = True
        return SummarySubmissionReceipt(
            accepted=True,
            code=None,
            missing=(),
            result=self.complete_result(),
        )

    def complete_result(self) -> dict[str, object] | None:
        if self._summary_text is None:
            return None
        return {"summary_text": self._summary_text}

    def verify_host_result(self, result: Mapping[str, object]) -> str:
        """Require the host result to be exactly the candidate staged here."""
        if not self._completed or set(result) != {"summary_text"}:
            raise SummaryResultMismatchError
        summary_text = result.get("summary_text")
        if not isinstance(summary_text, str) or summary_text != self._summary_text:
            raise SummaryResultMismatchError
        return summary_text

    def _history_row(self, message: HistoryMessage, source: str) -> dict[str, Any]:
        return {
            "message_id": message.message_id,
            "reply_to_message_id": message.reply_to_message_id,
            "timestamp": message.timestamp,
            "user_id": message.user_id,
            "nickname": message.nickname,
            "is_bot": message.user_id == self._bot_self_id,
            "source_type": "group_history",
            "source_id": source,
            "trust_level": "untrusted",
            "content": message.content[:MAX_ROW_CONTENT_CHARS],
        }

    @staticmethod
    def _page_payload(
        *,
        generation: int,
        offset: int,
        next_offset: int | None,
        total: int,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "generation": generation,
            "offset": offset,
            "next_offset": next_offset,
            "total": total,
            "rows": rows,
        }


__all__ = [
    "MAX_HISTORY_PAGE_BYTES",
    "MAX_HISTORY_PAGE_ROWS",
    "MAX_ROW_CONTENT_CHARS",
    "MissingRequirement",
    "SummaryCandidate",
    "SummaryCandidateError",
    "SummaryCompletedError",
    "SummaryPageRangeError",
    "SummaryPageTooLargeError",
    "SummaryResultMismatchError",
    "SummarySelection",
    "SummaryStaleGenerationError",
    "SummarySubmissionReceipt",
]
