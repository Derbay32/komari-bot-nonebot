"""判定引擎协议契约。

供消费方注解引擎实例，避免在契约层引用插件内部类型。协议只声明聊天与群总结
入口实际需要的三个既有方法（判定、单次交接、已确认送达观测）；不提供旧结果
适配层，也不在本层复制引擎行为。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from .jev import (
        DecisionMessage,
        DecisionRequest,
        DecisionResult,
        HandoffRoute,
    )


class DecisionEngineProtocol(Protocol):
    """消费方注解真实判定引擎的协议：判定、交接与已确认送达观测。"""

    async def evaluate(self, request: DecisionRequest) -> DecisionResult:
        """执行一次完整判定（同一分发复用同一份结果）。"""
        ...

    async def claim_handoff(
        self,
        request: DecisionRequest,
        result: DecisionResult,
        route: HandoffRoute,
    ) -> bool:
        """领取一次单次外部效果资格；同一事件最多成功一次。"""
        ...

    async def observe_delivered(
        self,
        message: DecisionMessage,
        *,
        protocol: str,
        account_id: str,
        received_at: datetime,
    ) -> None:
        """吸收一条已确认的平台送达（只更新判定自有上下文）。"""
        ...


__all__ = ["DecisionEngineProtocol"]
