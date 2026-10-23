"""单任务 TypeSafe SDK 客户端所有权。

判定业务按需进入 ``configured_jev_request`` 取得一个**请求作用域**的官方
``AsyncTypeSafeClient``，在其中直接调用 ``client.system_one(...)``，并只在上下文
正常退出后消费结果。本模块只负责：从统一配置快照生成 SDK 客户端、以请求总
截止主动取消超时请求、以及在成功、异常和取消时释放客户端资源。

有意不做的事情：不包装 ``system_one``、不实现请求协议或响应解析、不引入
provider 抽象、不读取 SDK 环境变量、不自动重试。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from komari_bot.plugins.komari_decision.config_schema import (
    normalize_jev_api_key,
    normalize_jev_base_url,
    normalize_jev_model,
    normalize_jev_timeout_seconds,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2

    from komari_bot.plugins.komari_decision.config_schema import (
        KomariDecisionConfigSchema,
    )

__all__ = [
    "JevRequestConfigurationError",
    "JevRequestError",
    "JevRequestTimeoutError",
    "configured_jev_request",
]

_SDK_LOGGER_NAME = "typesafe_sdk"
"""SDK 在 debug 级别记录请求与响应正文的 logger 名字（官方文档约定）。"""


class JevRequestError(RuntimeError):
    """本模块在 SDK 之外自行判定的请求失败。"""


class JevRequestConfigurationError(JevRequestError):
    """配置缺少可用值或快照畸形；在创建 SDK 客户端前失败，不回显被拒值。"""


class JevRequestTimeoutError(JevRequestError, TimeoutError):
    """请求总截止已过期；调用方不得使用本次判定的结果。"""


@dataclass(frozen=True)
class _JevRequestOptions:
    """首个 ``await`` 之前冻结的请求参数快照。"""

    base_url: str
    api_key: str
    model: str
    http_timeout_seconds: float
    request_timeout_seconds: float

    @classmethod
    def from_config(cls, config: KomariDecisionConfigSchema) -> _JevRequestOptions:
        """从统一配置拷贝五个标量并逐项校验。"""
        try:
            options = cls(
                base_url=normalize_jev_base_url(config.jev_base_url),
                api_key=normalize_jev_api_key(config.jev_api_key),
                model=normalize_jev_model(config.jev_model),
                http_timeout_seconds=normalize_jev_timeout_seconds(
                    config.jev_http_timeout_seconds,
                    field_name="jev_http_timeout_seconds",
                ),
                request_timeout_seconds=normalize_jev_timeout_seconds(
                    config.jev_request_timeout_seconds,
                    field_name="jev_request_timeout_seconds",
                ),
            )
            options.require_usable()
        except ValueError as error:
            raise JevRequestConfigurationError(str(error)) from error
        return options

    def require_usable(self) -> None:
        """要求地址、凭据与模型都已显式配置；只报告缺失字段名，不回显取值。"""
        missing = [
            field_name
            for field_name, value in (
                ("jev_base_url", self.base_url),
                ("jev_api_key", self.api_key),
                ("jev_model", self.model),
            )
            if not value
        ]
        if missing:
            msg = f"Jev 请求缺少可用配置: {', '.join(missing)}"
            raise JevRequestConfigurationError(msg)


def _disable_sdk_body_logging() -> None:
    """把 SDK logger 的最低级别抬到 INFO，关闭请求/响应正文输出。

    SDK 只在 debug 级别记录完整正文（``TYPESAFE_LOG_LEVEL=debug`` 或根 logger
    的 debug 设置都会打开它），而凭据只按 header 名字脱敏，正文并不脱敏。
    因此每次进入请求上下文都把 ``typesafe_sdk`` 的有效级别抬到 INFO：状态码
    与阶段行保留，正文不再产生。运维主动调到更高等级（WARNING 等）时不动。
    """
    sdk_logger = logging.getLogger(_SDK_LOGGER_NAME)
    if sdk_logger.getEffectiveLevel() < logging.INFO:
        sdk_logger.setLevel(logging.INFO)


def _deadline_message(options: _JevRequestOptions) -> str:
    return f"Jev 请求超过总截止 {options.request_timeout_seconds}s，结果不可用"


@asynccontextmanager
async def configured_jev_request(
    config: KomariDecisionConfigSchema,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[AsyncTypeSafeClient]:
    """为一次判定请求提供独占的官方 SDK 客户端。

    调用方必须在这个上下文里直接 ``await client.system_one(...)``，并只在上下文
    正常退出后使用结果（``async with`` 体之外）。请求总截止
    （``jev_request_timeout_seconds``）由 ``asyncio.timeout_at`` 主动施加：到期
    会取消 SDK 构造之后仍在进行的业务请求，并把这次取消转换成
    ``JevRequestTimeoutError``。它与 SDK 的 HTTP 单次操作超时
    （``jev_http_timeout_seconds``）是两件事：单次操作超时由 SDK 抛自己的错误，
    整体迟到由本上下文拒绝。上下文成功退出时还会再比对一次单调时钟，覆盖
    抑制取消、或在定时器到期后才完成同步校验的传输实现。本地截止不保证供应方
    停止计算或计费。

    Args:
        config: 统一配置快照；调用方可传入任务开始时冻结的实例。
        transport: SDK 支持的外部 httpx2 transport（测试或调用方注入）。

    Raises:
        JevRequestConfigurationError: 配置不可用，在创建 SDK 客户端前失败。
        JevRequestTimeoutError: 请求在总截止之后才结束，结果不可使用。
    """
    options = _JevRequestOptions.from_config(config)
    _disable_sdk_body_logging()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + options.request_timeout_seconds
    timeout_scope = asyncio.timeout_at(deadline)
    client: AsyncTypeSafeClient | None = None
    failure: BaseException | None = None
    try:
        async with timeout_scope:
            client = AsyncTypeSafeClient(
                api_key=options.api_key,
                base_url=options.base_url,
                model=options.model,
                timeout=options.http_timeout_seconds,
                retry=RetryPolicy(max_retries=0),
                transport=transport,
            )
            yield client
    except BaseException as error:
        # 原始失败（含调用方取消）优先：不得被换算成成功或超时。
        failure = error
        if isinstance(error, TimeoutError) and timeout_scope.expired():
            # 只转换本次总截止造成的取消/超时。
            raise JevRequestTimeoutError(_deadline_message(options)) from None
        raise
    finally:
        # 总截止只覆盖业务请求；释放发生在其外，不占用业务预算。
        expired = loop.time() >= deadline
        if client is not None:
            close_error = await _close(client)
            if failure is None and close_error is not None:
                raise close_error
    if expired:
        # 抑制了取消的传输或迟到完成的同步校验：仍然不接受结果。
        raise JevRequestTimeoutError(_deadline_message(options))


async def _close(client: AsyncTypeSafeClient) -> BaseException | None:
    """关闭本上下文独占的 SDK 客户端，返回关闭异常而不抛出。

    关闭在同一个任务里直接 await：取消会等它收尾，不留下未跟踪的后台清理
    任务；关闭失败也不覆盖已经发生的请求失败。
    """
    try:
        await client.aclose()
    except BaseException as error:
        return error
    return None
