"""TSK-230 持久群工作休眠/恢复共同矩阵 Adapter（测试专用装置）。

ADR-0012 要求同一休眠矩阵（admitted→开展业务；restricted→保存有效
revision 并休眠不耗失败；revision 变化 / failed→ready → 重裁决；不变→不
重复领取）复用于所有持久群工作。本 Adapter 表达该矩阵的共享参数形状，但每
个场景都必须驱动真实生产 worker / repository / manager 入口（见
``memories`` 各测试文件中的 harness），绝不成为自包含/自证模型。

``PersistentWorkScenario`` 是不变的输入描述；``run_scenario_once`` 接收一个
生产驱动 harness（闭包，内部调用真实生产入口）与断言回调，顺序执行一次并返
回场景可观察结果，由断言回调钉住休眠矩阵输出（AC10）。

矩阵维度：
- ``initial_revision``：候选发现时的 effective revision；
- ``qualifications``：按调息顺序的假配置…用``admitted``/``restricted``/
  ``failed`` 三态 token 表达；空 tuple 表示恒 `admitted`；
- ``mode``：``business`` 或 ``technical_cleanup``。
"""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003 -- 类型注解作用域
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PersistentWorkScenario:
    """一次持久群工作休眠/恢复场景的输入描述。"""

    scenario_id: str
    group_id: str
    ad_revision: int | None
    qualifications: tuple[str, ...] = ("admitted",)
    mode: str = "business"


def resolve_qualification(qualifications: tuple[str, ...], index: int) -> str:
    """按调用索引三态取资；耗尽后保持最后一项。"""
    return qualifications[min(index, len(qualifications) - 1)]


async def run_scenario(
    scenario: PersistentWorkScenario,
    production_harness: Callable[[], Any],
    *,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """运行生产 harness 并聚合场景观测结果。

    ``production_harness`` 必须在其闭包内调用真实生产
    worker/repository/manager 入口（例如 ``ConversationProcessingLifecycle``），
    并把观测写入 ``observations``；返回结果原样回传。
    """
    result = await production_harness()
    if observations is None:
        observations = {"result": result}
    observations.setdefault("scenario_id", scenario.scenario_id)
    return observations


__all__ = [
    "ADMITTED",
    "FAILED",
    "RESTRICTED",
    "PersistentWorkScenario",
    "resolve_qualification",
    "run_scenario",
]

ADMITTED = "admitted"
RESTRICTED = "restricted"
FAILED = "failed"
