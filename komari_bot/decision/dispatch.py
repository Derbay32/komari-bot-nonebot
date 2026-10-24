"""一次框架分发的共享判定载体（纯契约，不依赖框架或 SDK）。

同一框架分发内，群总结与普通聊天入口共享同一个 ``DecisionDispatch``：第一次
判定调用冻结本次请求与引擎身份，之后所有消费方只读取这一份结果，绝不重复
调用模型、重复抽签或二次交接。载体同时保留已完成结果，因此引擎**有界**结果
缓存之后的淘汰不会让共享消费方失去判定事实。

本模块不制造缺失状态、不提供兼容旧签名的适配层，也不承担通用工作流职责。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine_protocol import DecisionEngineProtocol
    from .jev import DecisionRequest, DecisionResult, HandoffRoute


class DecisionDispatchUnavailableError(RuntimeError):
    """当前框架分发没有可用的判定载体（故障关闭，不补造缺失状态）。"""


class DecisionDispatch:
    """一次分发内共享的冻结请求、首次引擎身份与保留的判定结果。"""

    __slots__ = ("_candidate", "_engine", "_request", "_result", "_task")

    def __init__(self, request: DecisionRequest) -> None:
        """以入站投影冻结的请求创建载体；分发标识由调用方冻结。"""
        self._request: DecisionRequest = request
        self._candidate: bool | None = None
        self._engine: DecisionEngineProtocol | None = None
        self._task: asyncio.Task[DecisionResult] | None = None
        self._result: DecisionResult | None = None

    @property
    def request(self) -> DecisionRequest:
        """本分发冻结的判定请求（首次判定后含冻结的候选标记）。"""
        return self._request

    @property
    def result(self) -> DecisionResult | None:
        """本次判定已保留的结果；尚未判定时为 ``None``。"""
        if self._result is not None:
            return self._result
        return self._completed_result()

    async def evaluate(
        self,
        engine: DecisionEngineProtocol,
        *,
        summary_candidate: bool,
    ) -> DecisionResult:
        """执行或复用本次分发唯一的一次判定。

        候选标记只在第一次调用时冻结；后续调用即使给出不同标记也返回第一次
        判定结果，且绝不再调用引擎。等待方被取消只影响该等待方，不影响共享
        任务本身。
        """
        if self._task is None:
            self._candidate = bool(summary_candidate)
            self._engine = engine
            self._request = replace(
                self._request,
                summary_candidate=self._candidate,
            )
            task = asyncio.get_running_loop().create_task(
                engine.evaluate(self._request),
                name=f"decision-dispatch-{self._request.dispatch_id}",
            )
            task.add_done_callback(self._retain_result)
            self._task = task
        return await asyncio.shield(self._task)

    async def claim_handoff(self, route: HandoffRoute) -> bool:
        """用本次分发的冻结请求与保留结果领取一次外部效果资格。

        不在本层判断资格、不改写路由，也不重新抽签：资格与重复事实完全由
        首次引擎的既有交接语义决定。
        """
        engine = self._engine
        result = self.result
        if engine is None or result is None:
            return False
        return bool(await engine.claim_handoff(self._request, result, route))

    def _retain_result(self, task: asyncio.Task[DecisionResult]) -> None:
        """在任务结束时保留结果；失败不伪造成任何判定事实。"""
        if task.cancelled():
            return
        try:
            self._result = task.result()
        except BaseException:  # 只登记已完成事实，不吞调用方异常
            return

    def _completed_result(self) -> DecisionResult | None:
        """任务已完成但回调尚未执行时的兜底读取（不产生第二次判定）。"""
        task = self._task
        if task is None or not task.done() or task.cancelled():
            return None
        try:
            return task.result()
        except BaseException:  # 失败任务没有可共享的判定事实
            return None


__all__ = ["DecisionDispatch", "DecisionDispatchUnavailableError"]
