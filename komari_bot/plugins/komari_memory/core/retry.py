"""异步重试装饰器。"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from nonebot import logger

T = TypeVar("T")


def retry_async(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
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
                except exceptions as e:
                    last_error = e
                    if attempt == max_attempts - 1:
                        logger.error(
                            f"[{func.__name__}] {max_attempts}次尝试全部失败: {last_error}"
                        )
                        raise

                    delay = min(base_delay * (2**attempt), max_delay)
                    logger.warning(
                        f"[{func.__name__}] 第{attempt + 1}次失败，{delay:.1f}秒后重试: {e}"
                    )
                    await asyncio.sleep(delay)

            # 理论上不会到达这里，但为了类型检查
            msg = "Unexpected state in retry logic"
            if last_error:
                raise last_error
            raise RuntimeError(msg)

        return wrapper  # type: ignore[return-value]

    return decorator
