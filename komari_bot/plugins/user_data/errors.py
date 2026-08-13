"""user_data 插件异常类型单一家园。

四个同族异常类型（两个服务不可用态 + 一个未就绪态 + 一个幂等冲突）统一
定义于此，``database`` 子模块与插件顶层入口均以 from-import 引用同一类
对象；``error_code`` 统一为带 :class:`typing.ClassVar` 注解的类属性，
供上游按结构化错误码分类。
"""

from __future__ import annotations

from typing import ClassVar


class UserDataDisabledError(RuntimeError):
    """user_data 已被动态配置关闭。"""

    error_code: ClassVar[str] = "service_unavailable"


class UserDataStoppingError(RuntimeError):
    """user_data 正在或已经关闭，不允许重新建立连接池。"""

    error_code: ClassVar[str] = "service_unavailable"


class UserDataUnavailableError(RuntimeError):
    """UserDataDB 连接池未初始化时抛出的异常。

    ``error_code == "service_unavailable"``，供上游（如 komari_chat
    承诺执行器）按结构化错误码分类；沿用既有 RuntimeError 语义。
    """

    error_code: ClassVar[str] = "service_unavailable"


class FavorabilityIdempotencyConflictError(ValueError):
    """好感度 operation_id 与既有请求载荷冲突时抛出的异常。

    ``error_code == "idempotency_conflict"``，供上游按结构化错误码
    区分幂等冲突；沿用既有 ValueError 语义。
    """

    error_code: ClassVar[str] = "idempotency_conflict"
