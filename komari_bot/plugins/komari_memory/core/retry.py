"""异步重试装饰器。"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from nonebot import logger

T = TypeVar("T")

# 尝试次数附着用的私有属性名：setattr/getattr 读写，对 pyright 友好（TSK-155）。
_RETRY_ATTEMPTS_ATTR = "_retry_attempts"


def get_retry_attempts(error: BaseException) -> int | None:
    """返回重试包装层附着在异常实例上的实际尝试次数（TSK-155）。

    穷尽重试后抛出附着 max_attempts；exclude 命中抛出附着已发生的实际尝试
    次数（首次命中为 1）；未经过 retry_async 包装层的异常返回 None。
    """
    attempts = getattr(error, _RETRY_ATTEMPTS_ATTR, None)
    return attempts if isinstance(attempts, int) else None


def retry_async(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    exclude: tuple[type[Exception], ...] = (),
) -> Callable[
    [Callable[..., Awaitable[T]]],
    Callable[..., Awaitable[T]],
]:
    """异步重试装饰器。

    Args:
        max_attempts: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
        exceptions: 需要重试的异常类型
        exclude: 命中即原样抛出的异常类型——不重试、不 sleep、不打重试日志，
            并附着已发生的实际尝试次数（首次命中为 1）

    Returns:
        装饰器函数
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """装饰器包装器。"""

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            """异步包装器。"""
            last_error: Exception | None = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exclude as e:
                    # exclude 命中：首次出现即原样抛出，不 sleep、不打重试日志；
                    # 附着已发生的实际尝试次数（TSK-155）。
                    setattr(e, _RETRY_ATTEMPTS_ATTR, attempt + 1)
                    raise
                except exceptions as e:
                    last_error = e
                    if attempt == max_attempts - 1:
                        setattr(e, _RETRY_ATTEMPTS_ATTR, max_attempts)
                        logger.error(
                            "[{}] {}次尝试全部失败: error_type={}",
                            func.__name__,
                            max_attempts,
                            type(last_error).__name__,
                        )
                        raise

                    delay = min(base_delay * (2**attempt), max_delay)
                    logger.warning(
                        "[{}] 第{}次失败，{:.1f}秒后重试: error_type={}",
                        func.__name__,
                        attempt + 1,
                        delay,
                        type(e).__name__,
                    )
                    await asyncio.sleep(delay)

            # 理论上不会到达这里，但为了类型检查
            msg = "Unexpected state in retry logic"
            if last_error:
                raise last_error
            raise RuntimeError(msg)

        return wrapper  # type: ignore[return-value]

    return decorator
