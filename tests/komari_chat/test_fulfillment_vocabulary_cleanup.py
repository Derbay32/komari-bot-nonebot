"""TSK-121: 履约链路日志与租约方法名词汇收尾验收测试。

验收对象（红线基线，实现落地前预期全红，防漂移锚点除外）：
- ``reply_fulfillment_workflow`` 内 9 处 ``operation={}`` 日志标签改为
  ``fulfillment={}``，中文正文里的「operation」措辞对齐为「履约」；
- ``ReplyFulfillmentRepository`` 方法改名：``has_active_operation`` ->
  ``has_fulfillment``、``claim_operation`` -> ``claim_lease``（并入既有
  ``renew_lease`` / ``release_lease`` 租约家族）；两处 Protocol 声明
  （``_ReplyFulfillmentRepository`` 与 ``_CommitmentExecutorRepository``）
  同步改名；
- 仓储模块 ``COMMITMENT_ORDER`` import 块下方与领域模块注释逐字重复的
  纪律回声注释删除。

防漂移锚点（当前即绿）：``renew_lease`` / ``release_lease`` 仍在仓储类
上（租约家族命名基线）。
"""

from __future__ import annotations

import inspect

from komari_bot.plugins.komari_chat.repositories import (
    reply_fulfillment_repository,
)
from komari_bot.plugins.komari_chat.services import (
    reply_commitment_workflow,
    reply_fulfillment_workflow,
)

WORKFLOW_LOG_TAG_COUNT = 9


def _workflow_source() -> str:
    return inspect.getsource(reply_fulfillment_workflow)


def _commitment_workflow_source() -> str:
    return inspect.getsource(reply_commitment_workflow)


def _repository_source() -> str:
    return inspect.getsource(reply_fulfillment_repository)


def _repository_class() -> object:
    repository_class = getattr(
        reply_fulfillment_repository, "ReplyFulfillmentRepository", None
    )
    assert repository_class is not None
    return repository_class


def _workflow_repository_protocol() -> object:
    protocol = getattr(
        reply_fulfillment_workflow, "_ReplyFulfillmentRepository", None
    )
    assert protocol is not None
    return protocol


def _commitment_executor_protocol() -> object:
    protocol = getattr(
        reply_commitment_workflow, "_CommitmentExecutorRepository", None
    )
    assert protocol is not None
    return protocol


def test_workflow_log_tags_use_fulfillment_label() -> None:
    """验收标准 1a：9 处 operation={} 日志标签全部改为 fulfillment={}。

    插值参数本就是 fulfillment_id，标签与措辞不得再保留 operation 争议名。
    """
    source = _workflow_source()
    assert "operation={" not in source
    assert source.count("fulfillment={") == WORKFLOW_LOG_TAG_COUNT


def test_workflow_log_wording_no_operation() -> None:
    """验收标准 1b：日志中文正文「operation」措辞对齐为「履约」。

    唯一含 operation 措辞的日志为重复回复拦截消息（原句
    「重复回复 operation 已存在」），实现落地后不得再出现该措辞。
    """
    source = _workflow_source()
    assert "operation 已存在" not in source


def test_repository_has_fulfillment_method() -> None:
    """验收标准 2a：仓储类 has_active_operation 改名 has_fulfillment。"""
    repository_class = _repository_class()
    has_fulfillment = getattr(repository_class, "has_fulfillment", None)
    assert has_fulfillment is not None
    assert getattr(repository_class, "has_active_operation", None) is None


def test_repository_has_claim_lease_method() -> None:
    """验收标准 2b：仓储类 claim_operation 改名 claim_lease。"""
    repository_class = _repository_class()
    claim_lease = getattr(repository_class, "claim_lease", None)
    assert claim_lease is not None
    assert getattr(repository_class, "claim_operation", None) is None


def test_workflow_protocol_has_fulfillment_method() -> None:
    """验收标准 2c：_ReplyFulfillmentRepository Protocol 同步改名。"""
    protocol = _workflow_repository_protocol()
    assert getattr(protocol, "has_fulfillment", None) is not None
    assert getattr(protocol, "has_active_operation", None) is None


def test_workflow_module_no_active_operation_identifier() -> None:
    """验收标准 2c 补：履约工作流模块内不再出现 has_active_operation。

    覆盖 Protocol 声明与 ``is_duplicate_event`` 调用点两处旧名；新名
    ``has_fulfillment`` 必须在模块源码中落地。
    """
    source = _workflow_source()
    assert "has_active_operation" not in source
    assert "has_fulfillment" in source


def test_commitment_protocol_has_claim_lease_method() -> None:
    """验收标准 2d：_CommitmentExecutorRepository Protocol 同步改名。"""
    protocol = _commitment_executor_protocol()
    assert getattr(protocol, "claim_lease", None) is not None
    assert getattr(protocol, "claim_operation", None) is None


def test_commitment_module_no_claim_operation_identifier() -> None:
    """验收标准 2d 补：承诺工作流模块内不再出现 claim_operation。

    覆盖 Protocol 声明与执行器调用点两处旧名；新名 ``claim_lease``
    必须在模块源码中落地。
    """
    source = _commitment_workflow_source()
    assert "claim_operation" not in source
    assert "claim_lease" in source


def test_repository_echo_comment_removed() -> None:
    """验收标准 3：仓储模块纪律回声注释删除。

    原注释与领域模块「唯一权威」注释逐字重复，关键句为
    「承诺固定顺序的唯一权威定义在领域模块」与
    「不各自定义私有副本」，实现落地后不得出现在仓储源码中。
    """
    source = _repository_source()
    assert "唯一权威定义在领域模块" not in source
    assert "不各自定义私有副本" not in source


def test_lease_family_methods_remain() -> None:
    """防漂移锚点：renew_lease / release_lease 仍存在于仓储类。

    当前即绿：租约家族命名基线（claim_lease 并入该家族后不得改名）。
    """
    repository_class = _repository_class()
    assert getattr(repository_class, "renew_lease", None) is not None
    assert getattr(repository_class, "release_lease", None) is not None
