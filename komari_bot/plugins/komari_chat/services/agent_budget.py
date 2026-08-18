"""回复 Agent 任务级冻结执行预算与工具调用约束模式（TSK-192 / TSK-193）。

预算三元组（最大轮次 / 单轮工具上限 / 整任务工具上限）与工具调用
约束模式（``required | prompt_guided``）在任务起点从配置读取一次并冻结
为不可变值对象；任务执行期间配置变更不影响进行中的任务，只作用于下
一个任务。普通 / debug / 简单三条入口共用同一份冻结快照。

计数规则：

- 每个逻辑轮次（一次模型 completion 请求，含无工具轮与瞬时重试后的成功
  轮）消耗 1 轮；
- 模型提出的全部 ``tool_calls`` 在进入单轮 / 总量校验前先计入总调用数，
  因此未知工具、坏参数、执行失败、favorability、final_response 以及被
  单轮上限拒绝的批次都消耗总预算；
- 任一批次超出单轮或总量上限时整批拒绝，绝不执行该批任何工具（无半批）。

本模块不依赖 NoneBot 运行时，只定义冻结值与消耗账本的深模块边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

#: 合法工具调用约束模式取值（无兼容值 / 宽松规范化）。
VALID_TOOL_CALL_MODES: frozenset[str] = frozenset({"required", "prompt_guided"})


@dataclass(frozen=True, slots=True)
class AgentExecutionBudget:
    """任务起点冻结的回复 Agent 执行预算与约束模式值对象。"""

    rounds: int
    per_round: int
    total: int
    tool_call_mode: Literal["required", "prompt_guided"] = "required"

    @classmethod
    def from_config(cls, config: object) -> "AgentExecutionBudget":
        """任务起点从配置读取一次并冻结；整个任务不再重读配置。

        预算字段来自 ``komari_chat`` 动态配置；缺失或非法时在任务起点
        立即失败，绝不静默回退到任何隐藏默认值（TSK-192）。工具调用
        约束模式同样来自 ``komari_chat`` 动态配置：非法值明确失败；
        字段缺失只可能来自未迁移的旧快照 / 测试替身（0014 之后生产 typed
        配置必然携带该字段），按默认 ``required`` 兼容处理（TSK-193）。
        """
        rounds = getattr(config, "agent_max_rounds", None)
        per_round = getattr(config, "agent_max_tool_calls_per_round", None)
        total = getattr(config, "agent_max_total_tool_calls", None)
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (rounds, per_round, total)
        ):
            msg = (
                "配置缺少回复 Agent 执行预算字段 "
                "（agent_max_rounds / agent_max_tool_calls_per_round / "
                "agent_max_total_tool_calls），无法冻结任务预算"
            )
            raise RuntimeError(msg)
        tool_call_mode = getattr(config, "agent_tool_call_mode", None)
        if tool_call_mode is None:
            tool_call_mode = "required"
        elif tool_call_mode not in VALID_TOOL_CALL_MODES:
            msg = (
                "配置的工具调用约束模式非法（agent_tool_call_mode="
                f"{tool_call_mode!r}），必须为 required 或 prompt_guided"
            )
            raise RuntimeError(msg)
        return cls(
            rounds=cast("int", rounds),
            per_round=cast("int", per_round),
            total=cast("int", total),
            tool_call_mode=cast(
                "Literal['required', 'prompt_guided']", tool_call_mode
            ),
        )


@dataclass(frozen=True, slots=True)
class BatchVerdict:
    """一批工具调用提出后的校验结论。"""

    accepted: bool
    reason: str | None = None
    #: 是否因总量超限而终止整个任务（True 时 caller 应停止循环）。
    terminal: bool = False


class AgentBudgetLedger:
    """任务内预算消耗账本：冻结预算 + 随任务推进的计数。

    实例生命周期即一个回复任务；任务结束（成功或失败）后丢弃。
    """

    __slots__ = ("_budget", "_rounds_used", "_tool_calls_used")

    def __init__(self, budget: AgentExecutionBudget) -> None:
        self._budget = budget
        self._rounds_used = 0
        self._tool_calls_used = 0

    @property
    def rounds_limit(self) -> int:
        return self._budget.rounds

    @property
    def per_round_limit(self) -> int:
        return self._budget.per_round

    @property
    def total_limit(self) -> int:
        return self._budget.total

    @property
    def rounds_used(self) -> int:
        return self._rounds_used

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls_used

    def consume_round(self) -> None:
        """登记一个逻辑轮次（发出该轮 completion 请求时调用一次）。"""
        self._rounds_used += 1

    def propose_batch(self, count: int) -> BatchVerdict:
        """登记一批提出的工具调用并校验。

        先计入总调用数，再依次校验总量与单轮上限；返回
        ``BatchVerdict.accepted=False`` 时该批任何工具都不得执行。
        """
        self._tool_calls_used += count
        if self._tool_calls_used > self._budget.total:
            return BatchVerdict(
                accepted=False,
                reason=(
                    f"工具预算上限：本批提出 {count} 个调用后总量将达 "
                    f"{self._tool_calls_used}，超过总预算 {self._budget.total}"
                ),
                terminal=True,
            )
        if count > self._budget.per_round:
            return BatchVerdict(
                accepted=False,
                reason=f"单轮工具调用数超过 {self._budget.per_round}",
            )
        return BatchVerdict(accepted=True)


__all__ = [
    "AgentBudgetLedger",
    "AgentExecutionBudget",
    "BatchVerdict",
]
