"""任务级图片理解会话（TSK-195 / ADR-0010）。

delegated 模式下，单个回复 Agent 任务拥有一个图片理解会话：

- 构建时零预下载；只有首次 ``read(index)`` 才经安全下载器懒下载该索引
  的图片，并交由独立视觉模型返回文字描述；
- 稳定引用、来源归属（引用消息/当前消息）与原来源序号在构建时固定，
  失败不删除、不压缩、不重编号；
- 成功与失败结果按索引缓存，同 index 重复读取复用同一结果；并发重复
  读取同一索引单飞共享同一请求（不重复网络/视觉调用）；
- 下载字节账本、并发信号量与累计总时限由会话（经 ``ImageDownloadSession``）
  统一拥有，跨多轮工具调用累计且不重建；会话实例之间完全隔离；
- 原始 URL 只存在于会话内部私有映射 → 安全下载器边界：公开引用投影只含
  稳定 index/origin/original_index/安全来源标签，不进入主模型 messages、
  工具结果、普通日志与 Agent Run/debug 投影；日志只保留安全来源标签/
  索引/计数；
- 提供对全部失败状态的安全摘要（模式/阶段/失败数量/归一化错误类型），
  供 TSK-196 后续消费；本模块不发送任何通知。

本模块不依赖 NoneBot 运行时，只定义任务级图片会话的深模块边界。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

from nonebot import logger

from .image_downloader import (
    ImageDownloadPolicy,
    ImageDownloadSession,
    safe_url_label,
)
from .vision_service import read_images

if TYPE_CHECKING:
    from collections.abc import Callable

    from komari_bot.onebot.group_failure_notify import ImageFailureDiagnostic
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

#: 图片来源归属：引用消息图片在前、当前消息图片在后。
ImageOrigin = Literal["quoted", "current"]

#: 归一化失败原因（TSK-196 摘要消费的稳定错误类型）。
ImageErrorType = Literal["invalid_index", "image_unavailable", "vision_failed"]

#: 失败阶段（下载/解码/SSRF 等归入 download；视觉调用归入 vision）。
ImageStage = Literal["invalid", "download", "vision"]


@dataclass(frozen=True, slots=True)
class ImageReference:
    """稳定图片引用（公开投影，不含原始 URL）。

    Attributes:
        index: 跨任务稳定索引（引用在前、当前在后，从 0 连续）。
        origin: 来源归属（quoted=被回复消息，current=当前消息）。
        original_index: 该来源原始图片列表中的序号（稳定，不因失败重编号）。
        source_label: 供日志/诊断的安全来源标签（scheme://host[:port]）。
    """

    index: int
    origin: ImageOrigin
    original_index: int
    source_label: str

    def __str__(self) -> str:
        """面向日志/诊断的表示：绝不包含原始 URL path/query/base64。"""
        return (
            f"ImageReference(index={self.index}, origin={self.origin}, "
            f"original_index={self.original_index}, source={self.source_label})"
        )


@dataclass(frozen=True, slots=True)
class ImageReadResult:
    """单张图片读取结果（成功描述或结构化失败）。

    原始 URL 与 base64 不进入结果对象：成功只带文字描述，失败只带结构化
    提示与归一化错误类型/阶段。
    """

    index: int
    status: Literal["success", "failure", "invalid_index"]
    description: str | None = None
    failure_message: str | None = None
    error_type: ImageErrorType | None = None
    stage: ImageStage | None = None


@dataclass(frozen=True, slots=True)
class ImageFailureSummary:
    """全部失败状态的安全摘要（TSK-196 消费；无 URL/base64/正文）。

    ``mode`` 区分图片理解模式：delegated 由任务级图片会话汇总，native 由
    聊天核心在批量下载/主 LLM 失败时汇总；两模式共用同一诊断投影。
    """

    mode: Literal["native", "delegated"] = "delegated"
    all_images_unavailable: bool = False
    total_images: int = 0
    attempted_images: int = 0
    failed_images: int = 0
    error_types: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()

    def to_diagnostic(self) -> "ImageFailureDiagnostic":
        """投影为 onebot 共享边界的窄诊断（仅白名单字段，无 URL/base64）。"""
        from komari_bot.onebot.group_failure_notify import ImageFailureDiagnostic

        return ImageFailureDiagnostic(
            mode=self.mode,
            failed_count=self.failed_images,
            stages=self.stages,
            error_types=self.error_types,
        )


def _as_failure(
    index: int,
    message: str,
    *,
    error_type: ImageErrorType,
    stage: ImageStage,
) -> ImageReadResult:
    return ImageReadResult(
        index=index,
        status="failure",
        failure_message=message,
        error_type=error_type,
        stage=stage,
    )


class ImageUnderstandingFailureError(RuntimeError):
    """图片理解失败专用异常；只携带安全摘要（无 URL/base64/正文）。

    TSK-196：delegated 全部可用索引均已尝试且失败、native 全部下载失败或
    带图请求主 LLM 失败时，以本异常终止回复任务；``str()`` 只含模式，
    通知/日志/Agent Run 不保留原异常正文或 cause。
    """

    def __init__(self, summary: ImageFailureSummary) -> None:
        self.summary = summary
        super().__init__(f"图片理解失败（mode={summary.mode}）")


@runtime_checkable
class ImageReadingSessionProtocol(Protocol):
    """回复工具循环消费的最小会话契约（窄 Protocol）。

    主循环请求/诊断只会见到稳定 ``image_index`` 与结构化结果；原始 URL、
    base64 与下载细节不越过该契约。
    """

    @property
    def total_count(self) -> int: ...

    async def read(
        self,
        index: int,
        *,
        parent_call_id: str | None = None,
    ) -> ImageReadResult: ...

    def all_images_unavailable(self) -> bool: ...

    def failure_summary(self) -> ImageFailureSummary: ...


class ImageReadingSession:
    """单个回复 Agent 任务的图片理解会话（delegated）。

    任务起点经 ``build`` 构造一次并把同一实例传给
    ``generate_reply_with_tools``；任务期间只按稳定 ``image_index`` 调用
    ``read()``。任务结束调用 ``close()`` 释放下载连接；会话不跨 QQ 消息
    复用，任务间实例完全隔离。
    """

    def __init__(
        self,
        references: list[ImageReference],
        sources: list[str],
        policy: ImageDownloadPolicy,
        *,
        vision_model: str,
        vision_temperature: float = 0.3,
        vision_max_tokens: int = 1024,
        vision_request_api: str = "chat_completions",
        vision_stream_enabled: bool = False,
        vision_thinking_mode: bool = False,
        vision_reasoning_effort: str = "",
        request_trace_id: str | None = None,
        collector: "LLMDiagnosticCollector | None" = None,
    ) -> None:
        self._references = list(references)
        #: 原始 URL 只存在于会话内部私有映射（index → source），仅传给安全
        #: 下载器；公开引用投影（``ImageReference``）不携带原始 URL。
        self._sources: dict[int, str] = dict(
            zip((ref.index for ref in references), sources, strict=True)
        )
        self._by_index: dict[int, ImageReference] = {
            ref.index: ref for ref in references
        }
        self._policy = policy
        self._downloader = ImageDownloadSession(policy)
        self._results: dict[int, ImageReadResult] = {}
        self._inflight: dict[int, asyncio.Task[ImageReadResult]] = {}
        self._closed = False
        self._vision_model = vision_model
        self._vision_temperature = vision_temperature
        self._vision_max_tokens = vision_max_tokens
        self._vision_request_api = vision_request_api
        self._vision_stream_enabled = vision_stream_enabled
        self._vision_thinking_mode = vision_thinking_mode
        self._vision_reasoning_effort = vision_reasoning_effort
        self._request_trace_id = request_trace_id
        self._collector = collector

    @classmethod
    def build(
        cls,
        *,
        quoted_sources: list[str],
        current_sources: list[str],
        policy: ImageDownloadPolicy,
        max_images: int | None = None,
        vision_model: str,
        vision_temperature: float = 0.3,
        vision_max_tokens: int = 1024,
        vision_request_api: str = "chat_completions",
        vision_stream_enabled: bool = False,
        vision_thinking_mode: bool = False,
        vision_reasoning_effort: str = "",
        request_trace_id: str | None = None,
        collector: "LLMDiagnosticCollector | None" = None,
    ) -> "ImageReadingSession":
        """从引用消息与当前消息的来源构造会话；索引稳定且引用在前。

        ``quoted_sources`` 在前、``current_sources`` 在后；超过
        ``max_images``（默认为 ``policy.max_images``）的来源不进入可读
        集合。构造本身零预下载。
        """
        effective_max = policy.max_images if max_images is None else max_images
        references: list[ImageReference] = []
        raw_sources: list[str] = []
        index = 0
        for origin in ("quoted", "current"):
            source_list = (
                quoted_sources if origin == "quoted" else current_sources
            )
            for original_index, source in enumerate(source_list):
                if index >= effective_max:
                    break
                references.append(
                    ImageReference(
                        index=index,
                        origin=cast("ImageOrigin", origin),
                        original_index=original_index,
                        source_label=safe_url_label(source),
                    )
                )
                raw_sources.append(source)
                index += 1
            if index >= effective_max:
                break
        return cls(
            references,
            raw_sources,
            policy,
            vision_model=vision_model,
            vision_temperature=vision_temperature,
            vision_max_tokens=vision_max_tokens,
            vision_request_api=vision_request_api,
            vision_stream_enabled=vision_stream_enabled,
            vision_thinking_mode=vision_thinking_mode,
            vision_reasoning_effort=vision_reasoning_effort,
            request_trace_id=request_trace_id,
            collector=collector,
        )

    @property
    def total_count(self) -> int:
        """可读取图片总数（稳定索引范围 0..total_count-1）。"""
        return len(self._references)

    @property
    def references(self) -> tuple[ImageReference, ...]:
        """稳定引用只读视图（公开投影不含原始 URL；原始 URL 只在会话内部
        私有映射 → 安全下载器边界）。"""
        return tuple(self._references)

    @property
    def quoted_count(self) -> int:
        """被回复消息中可读取的图片数量（引用在前，占 0..quoted-1）。"""
        return sum(1 for ref in self._references if ref.origin == "quoted")

    @property
    def current_count(self) -> int:
        """当前消息中可读取的图片数量（占 quoted..total-1）。"""
        return sum(1 for ref in self._references if ref.origin == "current")

    @property
    def downloaded_bytes(self) -> int:
        """本任务已累计下载的响应体字节数。"""
        return self._downloader.downloaded_bytes

    async def read(
        self,
        index: int,
        *,
        parent_call_id: str | None = None,
    ) -> ImageReadResult:
        """读取稳定 ``image_index`` 对应图片；成功/失败均缓存。

        并发重复读取同一索引单飞共享同一进行中的下载与视觉请求；任务级
        下载预算与总时限由会话内唯一的 ``ImageDownloadSession`` 持有。
        会话关闭后任何索引（即使关闭前已缓存成功/失败）都返回结构化会话
        关闭失败，且绝不触发下载或视觉调用。
        原始 URL 不越过本边界：主循环/工具结果/诊断只见索引与结构化结果。
        """
        reference = self._by_index.get(index)
        if reference is None:
            return ImageReadResult(
                index=index,
                status="invalid_index",
                failure_message=(
                    f"[图片读取失败: image_index={index} 超出范围，"
                    f"当前可读图片数量为 {len(self._references)}]"
                ),
                error_type="invalid_index",
                stage="invalid",
            )

        if self._closed:
            return _as_failure(
                index,
                "[图片读取失败: 图片读取会话已关闭]",
                error_type="image_unavailable",
                stage="download",
            )

        cached = self._results.get(index)
        if cached is not None:
            return cached

        task = self._inflight.get(index)
        if task is not None:
            # 加入已有单飞任务；asyncio.shield 保证当前 waiter 被取消时
            # 不会把取消传播到共享任务（Python 3.11+ 取消会沿 await 传递），
            # 其他 waiter 仍拿到同一结果。
            return await asyncio.shield(task)

        task = asyncio.create_task(
            self._read_one(
                reference,
                self._sources[index],
                parent_call_id=parent_call_id,
            )
        )
        self._inflight[index] = task
        task.add_done_callback(self._make_read_done_callback(index))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # 仅当前 waiter 被取消：共享单飞任务经 shield 继续执行，结果由
            # done callback 缓存；不得把 in-flight 条目弹出（其他 waiter 和
            # 新的 read 仍要加入同一任务）。
            raise
        except Exception:  # 防御性兜底：_read_one 已自行处理异常
            result = _as_failure(
                index,
                "[图片读取失败: 未知错误]",
                error_type="vision_failed",
                stage="vision",
            )
        self._results.setdefault(index, result)
        return result

    def _make_read_done_callback(
        self, index: int
    ) -> Callable[[asyncio.Task[ImageReadResult]], None]:
        """共享单飞任务完成回调：弹出 in-flight 并把结果（或结构化失败）缓存。

        无论是否有 waiter 正在 await 都会执行，保证唯一 waiter 被取消后
        任务完成仍缓存结果；被取消的任务不缓存。
        """

        def _on_done(task: asyncio.Task[ImageReadResult]) -> None:
            self._inflight.pop(index, None)
            if task.cancelled():
                return
            if task.exception() is None:
                self._results.setdefault(index, task.result())
            else:
                self._results.setdefault(
                    index,
                    _as_failure(
                        index,
                        "[图片读取失败: 未知错误]",
                        error_type="vision_failed",
                        stage="vision",
                    ),
                )

        return _on_done

    async def _read_one(
        self,
        reference: ImageReference,
        source: str,
        *,
        parent_call_id: str | None,
    ) -> ImageReadResult:
        """单张图片的懒下载 + 视觉描述；失败按索引缓存。

        安全异常边界按阶段拆分（TSK-195 语义修正）：下载阶段异常归
        ``image_unavailable``/``download``，视觉阶段异常归
        ``vision_failed``/``vision``，避免 TSK-196 安全摘要把下载故障误报
        为视觉故障；两阶段异常日志都只记录 index/scheme://host 标签与归一
        化异常类型，不捕获 traceback 也不记录 ``str(exc)``，CancelledError
        两阶段都继续传播。
        """
        index = reference.index
        logger.info(
            "[ImageReadingSession] 开始读取图片: index={} origin={} "
            "original={} source={}",
            index,
            reference.origin,
            reference.original_index,
            reference.source_label,
        )
        # 阶段 1：安全下载器懒下载。下载/解码/SSRF 等异常统一归入下载阶段，
        # 缓存为 image_unavailable/download，避免被 TSK-196 误报为视觉故障。
        try:
            data_uri = await self._downloader.download(source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[ImageReadingSession] 图片下载异常: index={} source={} "
                "error_type={}",
                index,
                reference.source_label,
                type(exc).__name__,
            )
            return _as_failure(
                index,
                "[图片读取失败: 图片下载或解码失败，无法读取该图片]",
                error_type="image_unavailable",
                stage="download",
            )
        if data_uri is None:
            logger.warning(
                "[ImageReadingSession] 图片下载或解码失败: index={} source={}",
                index,
                reference.source_label,
            )
            return _as_failure(
                index,
                "[图片读取失败: 图片下载或解码失败，无法读取该图片]",
                error_type="image_unavailable",
                stage="download",
            )

        # 阶段 2：独立视觉模型子调用。拒绝/超时/网络等异常统一归入视觉阶段，
        # 缓存为 vision_failed/vision。
        try:
            descriptions = await read_images(
                [data_uri],
                vision_model=self._vision_model,
                temperature=self._vision_temperature,
                max_tokens=self._vision_max_tokens,
                request_api=self._vision_request_api,
                stream_enabled=self._vision_stream_enabled,
                thinking_mode=self._vision_thinking_mode,
                reasoning_effort=self._vision_reasoning_effort,
                request_trace_id=(
                    self._request_trace_id if self._collector is not None else None
                ),
                parent_call_id=parent_call_id if self._collector is not None else None,
                collector=self._collector,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[ImageReadingSession] 图片视觉识别异常: index={} source={} "
                "error_type={}",
                index,
                reference.source_label,
                type(exc).__name__,
            )
            return _as_failure(
                index,
                "[图片读取失败: 未知错误]",
                error_type="vision_failed",
                stage="vision",
            )
        description = descriptions[0] if descriptions else ""
        if not description:
            return _as_failure(
                index,
                "[图片读取失败: 视觉服务未返回结果]",
                error_type="vision_failed",
                stage="vision",
            )
        if description.startswith("[图片读取失败:"):
            return _as_failure(
                index,
                description,
                error_type="vision_failed",
                stage="vision",
            )
        return ImageReadResult(
            index=index,
            status="success",
            description=description,
        )

    def all_images_unavailable(self) -> bool:
        """所有可用引用均已被尝试读取且全部失败。"""
        return self.failure_summary().all_images_unavailable

    def failure_summary(self) -> ImageFailureSummary:
        """对全部失败状态的安全摘要（无 URL/base64/正文，TSK-196 消费）。"""
        failures = [
            result
            for result in self._results.values()
            if result.status == "failure"
        ]
        total = len(self._references)
        attempted = len(self._results)
        all_failed = (
            total > 0
            and attempted >= total
            and all(result.status == "failure" for result in self._results.values())
        )
        # 稳定、确定性输出：按字典序去重，不依赖读取顺序（TSK-195 验收反馈）。
        error_types = tuple(
            sorted({result.error_type or "unknown" for result in failures})
        )
        stages = tuple(
            sorted({result.stage or "unknown" for result in failures})
        )
        return ImageFailureSummary(
            mode="delegated",
            all_images_unavailable=all_failed,
            total_images=total,
            attempted_images=attempted,
            failed_images=len(failures),
            error_types=error_types,
            stages=stages,
        )

    async def close(self) -> None:
        """关闭会话：阻止新读取、取消并等待在途任务、释放下载连接。

        幂等可重复调用；关闭后 ``read()`` 返回结构化失败且不产生任何下载
        或视觉调用，也不会在关闭后继续写诊断收集器。
        """
        if self._closed:
            await self._downloader.close()
            return
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        await self._downloader.close()


__all__ = [
    "ImageFailureSummary",
    "ImageReadResult",
    "ImageReadingSession",
    "ImageReadingSessionProtocol",
    "ImageReference",
    "ImageUnderstandingFailureError",
]
