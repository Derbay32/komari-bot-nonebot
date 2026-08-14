"""回复履约运维契约异常。

履约运维服务（komari_chat 侧）与受审计管理 API（komari_management
侧）共享同一组异常类，保证生产装配中 API 能按类型把运维失败映射为
409 / 404 / 422。管理插件只经本契约模块与 komari_chat 顶层窄 seam
消费 komari_chat 的能力，不 deep import services / repositories /
handlers。
"""

from __future__ import annotations


class ReplyFulfillmentOpsError(Exception):
    """回复履约运维操作失败基类。"""


class ReplyFulfillmentOpsConflictError(ReplyFulfillmentOpsError):
    """状态冲突或送达证据冲突，由管理 API 映射为 HTTP 409。"""


class ReplyFulfillmentOpsNotFoundError(ReplyFulfillmentOpsError):
    """履约身份不存在，由管理 API 映射为 HTTP 404。"""


class ReplyFulfillmentOpsValidationError(ReplyFulfillmentOpsError):
    """处置请求不合法（如动态承诺类型），由管理 API 映射为 HTTP 422。"""


__all__ = [
    "ReplyFulfillmentOpsConflictError",
    "ReplyFulfillmentOpsError",
    "ReplyFulfillmentOpsNotFoundError",
    "ReplyFulfillmentOpsValidationError",
]
