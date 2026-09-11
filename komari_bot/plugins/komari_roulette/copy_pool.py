"""冻结文案池：闭集结果键、模板校验与不可变快照（TSK-279）。

设计约束（TSK-266 1A/1F/1G、TSK-269 §1、TSK-279 A4-A9）：

- 只有「成功动作结果句」与 completed 终局两类文案可配置；等候取消、
  最后退出、等候超时以及全部错误文案继续固定在渲染层代码内。
- 键集合是闭集：多一个键或少一个键都在构造快照时失败，运行期不会出现
  无文案或未定义结果。
- 模板只允许声明的具名占位符；属性、索引、位置字段、自动编号、转换
  （``!r``）与格式说明（``:>5``）一律拒绝；原生提及结构（``<qqbot-at-user``、
  ``<@`` 以及裸 ``<``/``>``）同样拒绝，真实提及只能由渲染层注入。
- 长度校验复用 :data:`komari_bot.llm.content_budget.CONTENT_TEXT_BUDGET`，
  不新增猜测的 QQ 长度门槛，也不在各插件复制限额。

本模块不依赖 NoneBot、数据库或任何全局可变状态，因此既能被按源文件加载的
``config_schema`` 使用，也能被 QQ 渲染层共享。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from string import Formatter
from types import MappingProxyType
from typing import Protocol

from komari_bot.llm.content_budget import (
    CONTENT_TEXT_BUDGET,
    normalize_required_text,
)

__all__ = [
    "ACTION_COPY_KEYS",
    "ACTION_TEMPLATE_PLACEHOLDERS",
    "DEFAULT_ACTION_COPY_POOL",
    "DEFAULT_FINAL_COPY_POOL",
    "FINAL_ALLOWED_PLACEHOLDERS",
    "FINAL_COPY_KEYS",
    "FINAL_REQUIRED_PLACEHOLDERS",
    "CopyPoolSnapshot",
    "CopyPoolValidationError",
    "CopyRandomSource",
    "compile_copy_pool",
    "default_copy_snapshot",
    "validate_template",
]


class CopyPoolValidationError(ValueError):
    """文案模板或文案池不满足 TSK-279 契约。"""


class CopyRandomSource(Protocol):
    """隔离的文案随机源：只从给定的非空模板序列里选一条。

    与领域 ``RandomSource``（弹仓/道具）刻意分离：文案展示随机不得消耗
    领域随机序列，也不得被领域随机失败影响。
    """

    def choice(self, options: Sequence[str]) -> str:
        """返回 ``options`` 中的一条模板。"""
        ...


# ---------------------------------------------------------------------------
# 闭集键与占位符
# ---------------------------------------------------------------------------

#: 可配置的成功动作结果句键（闭集）。键名与领域结果码一致，唯一例外是
#: 空弹/实弹射击动作：结果码 ``shot`` 已被终局原因占用，动作侧使用
#: ``shoot``（见渲染层的 ``RESULT_CODE_COPY_KEYS``）。
ACTION_COPY_KEYS: frozenset[str] = frozenset(
    {
        "created",
        "joined",
        "left",
        "host_transferred",
        "started",
        "shoot",
        "reloaded",
        "turn_ended",
        "forfeited",
        "item_used",
        "item_discarded",
        "item_choice_pending",
        "item_choice_updated",
        "lock_used",
    }
)

#: 可配置的 ```completed``` 终局原因键（闭集），与领域
#: ``_eliminate_current(completion_reason=...)`` 取值一致。
FINAL_COPY_KEYS: frozenset[str] = frozenset({"shot", "forfeit", "timeout"})

#: 每个动作结果句允许的占位符。
#:
#: - 绝大多数动作句由渲染层提供 ``{name}``（动作发起者的冻结显示名）与
#:   ``{kind}``（本次消耗的弹种中文）；
#: - ``host_transferred`` 的 ``{name}`` 是**变更后**的局主冻结显示名，
#:   因为显式转让与局主退出都会落到同一结果码；
#: - ``lock_used`` 单独给出 ``{actor}`` / ``{target}`` / ``{tag}``，其中
#:   ``{tag}`` 是渲染层注入的受控提及占位，配置无法伪造它。
ACTION_TEMPLATE_PLACEHOLDERS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "created": frozenset({"name"}),
        "joined": frozenset({"name"}),
        "left": frozenset({"name"}),
        "host_transferred": frozenset({"name"}),
        "started": frozenset({"name"}),
        "shoot": frozenset({"name", "kind"}),
        "reloaded": frozenset({"name", "kind"}),
        "turn_ended": frozenset({"name", "kind"}),
        "forfeited": frozenset({"name"}),
        "item_used": frozenset({"name"}),
        "item_discarded": frozenset({"name"}),
        "item_choice_pending": frozenset({"name", "kind"}),
        "item_choice_updated": frozenset({"name", "kind"}),
        "lock_used": frozenset({"actor", "target", "tag"}),
    }
)

#: 终局模板允许出现的占位符。
FINAL_ALLOWED_PLACEHOLDERS: frozenset[str] = frozenset({"winner", "event", "wins"})

#: 终局模板必须同时表达的事实：唯一胜者、导致终局的事件、结算后累计胜场。
FINAL_REQUIRED_PLACEHOLDERS: frozenset[str] = frozenset(
    {"winner", "event", "wins"}
)

#: 默认成功动作文案池（单条模板，保持 TSK-278 定稿逐字输出）。
DEFAULT_ACTION_COPY_POOL: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "created": ("新局已创建。",),
        "joined": ("你已加入本局。",),
        "left": ("你已退出本局。",),
        "host_transferred": ("本局局主已变更为{name}。",),
        "started": ("游戏开始，{name}先手。",),
        "shoot": ("{name}打出一发{kind}。",),
        "reloaded": ("{name}装填了一发。",),
        "turn_ended": ("{name}结束了回合。",),
        "forfeited": ("{name}选择弃权。",),
        "item_used": ("道具已使用。",),
        "item_discarded": ("已丢弃道具。",),
        "item_choice_pending": ("你获得了新道具。",),
        "item_choice_updated": ("奖励选择已更新。",),
        "lock_used": ("{actor}对{target}（{tag}）使用了锁。",),
    }
)

#: 默认终局文案池（按终局原因分键，默认同一条普通单段模板）。
DEFAULT_FINAL_COPY_POOL: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "shot": ("{event}{winner} 获胜，累计胜场 {wins}。",),
        "forfeit": ("{event}{winner} 获胜，累计胜场 {wins}。",),
        "timeout": ("{event}{winner} 获胜，累计胜场 {wins}。",),
    }
)

#: 单行普通文本禁止的排版结构（Markdown 粗体/分隔线、块引用、列表前缀）、
#: 换行与控制字符，以及一切原生提及/XML 标记。
_FORBIDDEN_LAYOUT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("换行、制表符或控制字符", re.compile(r"[\x00-\x1f\x7f]")),
    ("Markdown 粗体或分隔线标记", re.compile(r"\*\*")),
    ("Markdown 块引用标记", re.compile(r"(?m)^\s*>")),
    ("Markdown 列表前缀", re.compile(r"(?m)^\s*(?:[-*+]\s|\d+[.、)]\s)")),
    ("原生提及或 XML 标记", re.compile(r"[<>]")),
)

_FORMATTER = Formatter()


# ---------------------------------------------------------------------------
# 模板校验
# ---------------------------------------------------------------------------


def _collect_placeholders(
    template: str,
    *,
    allowed_placeholders: frozenset[str],
) -> list[str]:
    """解析模板中的替换字段，拒绝一切绕过 ``str.format`` 命名占位符的写法。"""

    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as error:
        message = f"文案模板的花括号不合法：{template!r}"
        raise CopyPoolValidationError(message) from error

    names: list[str] = []
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name.isidentifier() or field_name.startswith("_"):
            message = f"文案模板只允许具名占位符，不允许 {field_name!r}"
            raise CopyPoolValidationError(message)
        if field_name not in allowed_placeholders:
            message = f"文案模板不允许占位符 {{{field_name}}}"
            raise CopyPoolValidationError(message)
        if conversion is not None:
            message = f"文案模板不允许转换写法 !{conversion}"
            raise CopyPoolValidationError(message)
        if format_spec:
            message = f"文案模板不允许格式说明 {format_spec!r}"
            raise CopyPoolValidationError(message)
        names.append(field_name)
    return names


def validate_template(
    template: str,
    *,
    allowed_placeholders: frozenset[str],
    final: bool = False,
) -> str:
    """校验一条配置文案模板，返回去首尾空白后的原文。

    与 TSK-279 §1 一致：只接受单行普通文本与声明的具名占位符；终局模板额外
    必须同时含 ``{winner}``、``{event}``、``{wins}``，且各出现恰好一次，保证
    渲染层能把真实提及注入到胜者名字之后。
    """

    if not isinstance(template, str):
        message = "轮盘文案模板必须是字符串"
        raise CopyPoolValidationError(message)

    text = normalize_required_text(
        template,
        label="轮盘文案模板",
        budget=CONTENT_TEXT_BUDGET,
    )
    for label, pattern in _FORBIDDEN_LAYOUT:
        if pattern.search(text):
            message = f"轮盘文案模板不能包含{label}：{text!r}"
            raise CopyPoolValidationError(message)

    names = _collect_placeholders(text, allowed_placeholders=allowed_placeholders)
    if final:
        missing = sorted(FINAL_REQUIRED_PLACEHOLDERS - set(names))
        if missing:
            placeholders = "、".join(f"{{{name}}}" for name in missing)
            message = f"终局文案模板必须同时表达 {placeholders}"
            raise CopyPoolValidationError(message)
        duplicated = sorted(
            name
            for name in FINAL_REQUIRED_PLACEHOLDERS
            if names.count(name) != 1
        )
        if duplicated:
            placeholders = "、".join(f"{{{name}}}" for name in duplicated)
            message = f"终局文案模板的 {placeholders} 必须各出现一次"
            raise CopyPoolValidationError(message)
    return text


# ---------------------------------------------------------------------------
# 不可变快照与编译器
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CopyPoolSnapshot:
    """已校验、不可变的文案快照：每个键都是非空模板 tuple。"""

    action_templates: Mapping[str, tuple[str, ...]]
    final_templates: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "action_templates",
            MappingProxyType(dict(self.action_templates)),
        )
        object.__setattr__(
            self,
            "final_templates",
            MappingProxyType(dict(self.final_templates)),
        )


def _compile_pool(
    pool: Mapping[str, Sequence[str]],
    *,
    expected_keys: frozenset[str],
    label: str,
    final: bool,
) -> dict[str, tuple[str, ...]]:
    if not isinstance(pool, Mapping):
        message = f"{label}必须是「结果键 → 非空模板列表」映射"
        raise CopyPoolValidationError(message)

    actual_keys = set(pool)
    missing = sorted(expected_keys - actual_keys)
    unknown = sorted(actual_keys - expected_keys)
    if missing or unknown:
        message = (
            f"{label}键集合必须与闭集一致"
            f"（缺少 {missing}，未知 {unknown}）"
        )
        raise CopyPoolValidationError(message)

    compiled: dict[str, tuple[str, ...]] = {}
    for key in sorted(expected_keys):
        raw_templates = pool[key]
        if isinstance(raw_templates, (str, bytes, bytearray)) or not isinstance(
            raw_templates, Sequence
        ):
            message = f"{label}[{key}] 必须是模板列表"
            raise CopyPoolValidationError(message)
        if not raw_templates:
            message = f"{label}[{key}] 至少需要一条模板"
            raise CopyPoolValidationError(message)
        allowed = (
            FINAL_ALLOWED_PLACEHOLDERS
            if final
            else ACTION_TEMPLATE_PLACEHOLDERS[key]
        )
        compiled[key] = tuple(
            validate_template(
                template,
                allowed_placeholders=allowed,
                final=final,
            )
            for template in raw_templates
        )
    return compiled


def compile_copy_pool(
    action_copy_pool: Mapping[str, Sequence[str]],
    final_copy_pool: Mapping[str, Sequence[str]],
) -> CopyPoolSnapshot:
    """校验并冻结一份文案池；任何键集合偏差或非法模板都直接失败。"""

    return CopyPoolSnapshot(
        action_templates=_compile_pool(
            action_copy_pool,
            expected_keys=ACTION_COPY_KEYS,
            label="动作文案池",
            final=False,
        ),
        final_templates=_compile_pool(
            final_copy_pool,
            expected_keys=FINAL_COPY_KEYS,
            label="终局文案池",
            final=True,
        ),
    )


def default_copy_snapshot() -> CopyPoolSnapshot:
    """代码内默认文案池编译出的不可变快照（无配置来源）。"""

    return compile_copy_pool(DEFAULT_ACTION_COPY_POOL, DEFAULT_FINAL_COPY_POOL)
