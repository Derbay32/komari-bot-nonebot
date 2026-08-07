"""判定引擎协议契约。

供消费方注解引擎实例，避免在契约层引用插件内部类型。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .decision_engine import DecisionOutcome


class DecisionEngineProtocol(Protocol):
    """消费方注解引擎实例的协议：仅声明 evaluate 协程签名。"""

    async def evaluate(
        self,
        *,
        message_content: str,
        group_id: str,
        at_trigger: bool,
    ) -> DecisionOutcome:
        """执行完整判定流程。"""
        ...


__all__ = ["DecisionEngineProtocol"]
