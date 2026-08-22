"""TSK-225 聊天即时效果准入测试共享基础设施（测试专用，不承载生产语义）。

提供把 ``komari_bot.plugins.group_admission`` 顶层 ``adjudicate`` 替换为
可控脚本的三态装置，以及一个失败即中断（fail-if-called）的异步方法包装器，
用于逐效果验收「restricted / failed 时下游不再执行」。

设计约定（ADR-0012）：

- 业务插件消费准入只经顶层 ``adjudicate`` / ``get_runtime_state`` 同步面；
  TSK-225 的目标接缝是 komari_chat 及其直接编排的瞬时效果在最接近业务目的
  处、在效果前最后一个同步步骤调用它，且一次裁决只授权紧随其后的一个不可
  分效果（不跨效果缓存）。
- 当前生产 komari_chat 尚未接入准入，因此「效果前必须裁决、拒绝时下游必须
  fail-if-called」的验收用例都以红态失败，证明接入缺失。
"""

from __future__ import annotations

from typing import Any

from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
)

ADMIT_STATES = frozenset({"admitted", "restricted", "failed"})


def result_for_state(state: str) -> AdmissionResult:
    """生成脚本 token 对应的真实 ``AdmissionResult`` 资格。

    ``state`` 只取 ``admitted`` / ``restricted`` / ``failed``：failed 无有效
    快照（``effective_revision is None``），restricted 有快照但被拒绝。
    """
    if state == "admitted":
        return AdmissionResult(
            qualification=AdmissionQualification.BUSINESS,
            effective_revision=1,
            reason_code="policy_admitted",
        )
    if state == "failed":
        return AdmissionResult(
            qualification=AdmissionQualification.REJECTED,
            effective_revision=None,
            reason_code="effective_policy_unavailable",
        )
    return AdmissionResult(
        qualification=AdmissionQualification.REJECTED,
        effective_revision=1,
        reason_code="policy_restricted",
    )


class ScriptedAdjudicate:
    """替身顶层 ``adjudicate``：记录调用并返回可配置三态结果。

    ``calls`` 记录每次 (关联群, intent)，断言「效果前已复查且归属/意图正
    确」；状态可在任务进行中经 ``set_state`` 切换，用于「已开始效果可完成、
    下一效果被拒」的多时点接缝验收。
    """

    def __init__(self, state: str = "admitted") -> None:
        if state not in ADMIT_STATES:
            raise ValueError(f"未知脚本状态: {state}")  # noqa: TRY003
        self.state = state
        self.calls: list[tuple[object, object]] = []

    def set_state(self, state: str) -> None:
        if state not in ADMIT_STATES:
            raise ValueError(f"未知脚本状态: {state}")  # noqa: TRY003
        self.state = state

    def __call__(
        self,
        associated_group_ids: object,
        *,
        intent: object = None,
        **kwargs: object,
    ) -> AdmissionResult:
        del kwargs
        effective_intent = (
            intent if intent is not None else AdmissionIntent.BUSINESS
        )
        self.calls.append((associated_group_ids, effective_intent))
        return result_for_state(self.state)


def install_scripted_adjudicate(
    monkeypatch: Any,
    scripted: ScriptedAdjudicate,
) -> None:
    """把替身安装到 ``group_admission`` 包顶层命名空间并挂 ``get_runtime_state``。

    替换包级 ``adjudicate`` 与 ``get_runtime_state`` 符号（生产调用方走顶层
    暴露面，见 ADR-0006）；恢复由 ``monkeypatch`` 生命周期自动完成。
    """

    # 模块级 import 会触发包 __init__ 顶层 require("config_manager")，在部分
    # 测试导入期 NoneBot 未就绪时中断，故在安装点惰性 import（测试专属 seam）。
    import komari_bot.plugins.group_admission as admission_package

    monkeypatch.setattr(admission_package, "adjudicate", scripted)
    monkeypatch.setattr(admission_package, "get_runtime_state", _stub_runtime_state)


def _stub_runtime_state() -> object:
    """替身 ``get_runtime_state``：始终给出 READY 投影。

    只让未来生产 path 可解析；真实运行时会话健康由 ``runtime_support`` 验收。
    """
    from komari_bot.plugins.group_admission.contracts import (
        AdmissionRuntimeState,
        AdmissionRuntimeStatus,
    )

    return AdmissionRuntimeState(
        status=AdmissionRuntimeStatus.READY,
        problem_code=None,
        configured_revision=1,
        effective_revision=1,
        using_last_known_good=False,
    )


def fail_if_called(context: str) -> Any:
    """返回调用即抛 ``AssertionError`` 的异步哨兵。

    用作效果下游的 fail-if-called 替身；``context`` 写入错误消息以归因。
    """

    async def _guard(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError(f"fail-if-called: 在 {context} 的下游仍被执行")  # noqa: TRY003

    return _guard
