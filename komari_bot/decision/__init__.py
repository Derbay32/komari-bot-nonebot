"""Komari Decision 判定契约共享包。

承载当前 Jev 运行时需要跨插件共享的三个纯契约：判定引擎协议、一次分发的
共享载体与分发不可用错误。

类型化请求/结果/诊断数据契约保留在 ``komari_bot.decision.jev``，由消费方直接
import；本包不再提供旧场景 DTO 或宽重排兼容导出。

本包零依赖 nonebot / redis / 插件内部类型，可被任意消费方安全 import。
"""

from .dispatch import DecisionDispatch, DecisionDispatchUnavailableError
from .engine_protocol import DecisionEngineProtocol

__all__ = [
    "DecisionDispatch",
    "DecisionDispatchUnavailableError",
    "DecisionEngineProtocol",
]
