"""TSK-223 阶段 B 可观测性测试共享基础设施（测试专用，不承载生产语义）。

承载无内容可观测性（窗口 / 故障期 / 通知）验收所需的确定性装配：

- ``FakeUtcClock``：可控 UTC 时钟 callable，经冻结的 ``_AdmissionRuntime``
  可选内部 DI 关键字 ``clock`` 注入，驱动 5 分钟归属窗口、30 分钟提醒、
  60 秒稳定恢复与全部状态时间戳；测试全程无真实 sleep；
- ``FakeAdmissionBot`` / ``MutableBotsProvider`` / ``MutableSuperusersProvider``：
  通知投递环境。生产经 ``online_bots_provider`` / ``superusers_provider``
  两个可选内部 DI 关键字消费，不得要求测试注入 logger 或通知器对象；
- ``capture_admission_logs``：Loguru sink 捕获 NoneBot 真实结构化日志
  record，按 ``extra.event`` 前缀 ``group_admission.`` 过滤；
- status 完整投影与低基数 telemetry 的冻结键集 / 闭集常量、UTC RFC 3339
  时间断言辅助。

冻结的生产 seam 语义（生产实现必须满足，否则用例红）：

- ``_AdmissionRuntime`` 继续允许无参构造；``clock`` / ``online_bots_provider``
  / ``superusers_provider`` 是正常内部 DI（不是 ``_test_hook``），缺省时生
  产使用真实默认（真实 UTC、真实在线 Bot 枚举、真实 SUPERUSER 配置）；
- 私有 ``async process_observability()``：以当前 clock 处理 5 分钟窗口、
  30 分钟提醒、60 秒稳定恢复并尽力 flush 内存待投通知；供后续生产
  scheduler 调用，不作顶层公开；
- 私有 ``record_private_input_rejected(*, event_family)``：只记账，静默；
- 私有 ``adjudicate`` 接受关键字 ``event_family``（默认 ``"unknown"``），
  非法 family 归一到 ``unknown``；顶层公开 ``adjudicate`` 签名不暴露该参数；
- 不公开 telemetry / notifier / getter / clock setter / reset / Fake /
  Protocol；测试只经 ``/status``、结构化日志 sink、fake Bot 调用与上述私有
  运行时生产 seam 观察。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

#: status 响应精确顶层键集（TSK-217 冻结：5 个运行时字段 + 5 个时间 + telemetry）。
EXPECTED_STATUS_KEYS = frozenset(
    {
        "status",
        "problem_code",
        "configured_revision",
        "effective_revision",
        "using_last_known_good",
        "configured_updated_at",
        "effective_loaded_at",
        "last_refresh_attempt_at",
        "last_storage_success_at",
        "problem_since",
        "telemetry",
    }
)

#: telemetry 子对象精确键集。
EXPECTED_TELEMETRY_KEYS = frozenset(
    {
        "started_at",
        "adjudications_total",
        "by_reason_code",
        "by_intent",
        "attribution_failures_by_event_family",
        "runtime_problem_occurrences",
    }
)

#: 7 个冻结准入结果原因码（低基数 map 的封闭键集）。
CLOSED_REASON_CODES = frozenset(
    {
        "policy_admitted",
        "policy_restricted",
        "group_attribution_unavailable",
        "effective_policy_unavailable",
        "fact_finalization_granted",
        "technical_cleanup_granted",
        "private_input_rejected",
    }
)

#: 3 个冻结行为目的（低基数 map 的封闭键集）。
CLOSED_INTENTS = frozenset(
    {"business", "fact_finalization", "technical_cleanup"}
)

#: 4 个冻结事件族（归属失败低基数 map 的封闭键集）。
CLOSED_EVENT_FAMILIES = frozenset({"message", "notice", "request", "unknown"})

#: 4 个冻结运行时问题码（problem occurrence map 的封闭键集）。
CLOSED_PROBLEM_CODES = frozenset(
    {
        "storage_unavailable",
        "stored_policy_invalid",
        "snapshot_publish_failed",
        "internal_error",
    }
)

#: UTC RFC 3339 时间序列化形态（秒精度或微秒精度，Z / +00:00 后缀）。
#: MULTILINE 使 finditer 在多行卡文本中逐行匹配；fullmatch 用法不受影响。
RFC3339_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$",
    re.MULTILINE,
)

#: 归属聚合窗口长度（秒）与故障期参数（冻结：5 分钟窗口 / 30 分钟提醒 /
#: 60 秒稳定恢复）。
ATTRIBUTION_WINDOW_SECONDS = 300
REMINDER_INTERVAL_SECONDS = 1800
RECOVERY_STABILITY_SECONDS = 60

#: 结构化日志固定事件名（extra.event）。
EVENT_RUNTIME_FAILED = "group_admission.runtime_failed"
EVENT_RUNTIME_DEGRADED = "group_admission.runtime_degraded"
EVENT_RUNTIME_REMINDER = "group_admission.runtime_reminder"
EVENT_RUNTIME_RECOVERED = "group_admission.runtime_recovered"
EVENT_ATTRIBUTION_UNAVAILABLE = "group_admission.attribution_unavailable"
EVENT_STALE_REVISION_IGNORED = "group_admission.stale_revision_ignored"
EVENT_POLICY_PUBLISHED = "group_admission.policy_published"

ALL_ADMISSION_EVENTS = frozenset(
    {
        EVENT_RUNTIME_FAILED,
        EVENT_RUNTIME_DEGRADED,
        EVENT_RUNTIME_REMINDER,
        EVENT_RUNTIME_RECOVERED,
        EVENT_ATTRIBUTION_UNAVAILABLE,
        EVENT_STALE_REVISION_IGNORED,
        EVENT_POLICY_PUBLISHED,
    }
)

#: 运行时类日志（含 reminder / stale revision）extra 键精确白名单。
RUNTIME_EXTRA_WHITELIST = frozenset(
    {
        "event",
        "status",
        "problem_code",
        "configured_revision",
        "effective_revision",
        "using_last_known_good",
        "duration_seconds",
        "occurrence_count",
        "suppressed_count",
    }
)

#: 归属日志在白名单基础上额外允许 event_family / window_seconds。
ATTRIBUTION_EXTRA_WHITELIST = RUNTIME_EXTRA_WHITELIST | {
    "event_family",
    "window_seconds",
}

#: 策略发布日志 extra 精确键集。
POLICY_EXTRA_KEYS = frozenset(
    {"event", "old_revision", "new_revision", "policy_fingerprint", "source"}
)

#: policy_published source 封闭值集。
POLICY_SOURCES = frozenset({"startup", "watcher", "management"})

#: SUPERUSER 故障/恢复私聊卡「key: value」字段白名单（TSK-217 §7 冻结）。
#: fault_start / runtime_reminder 恒允许这些键；recovered / combined 额外允许
#: resolved_at。occurrence_count 或任何其他键均不允许。
FAULT_CARD_FIELDS = frozenset(
    {
        "event",
        "status",
        "problem_code",
        "configured_revision",
        "effective_revision",
        "using_last_known_good",
        "duration_seconds",
        "started_at",
    }
)
FAULT_RESOLVED_CARD_FIELDS = FAULT_CARD_FIELDS | {"resolved_at"}

#: 明确点名的禁止字段（供断言失败消息可读）。
FAULT_CARD_FORBIDDEN_KEY = "occurrence_count"

#: offline fault→recovery 合并卡首行固定文案（非 ``key: value`` 行，解析时跳过）。
COMBINED_CARD_BANNER = "群聊准入运行时故障期间发生且现已恢复"

_EVENT_PREFIX = "group_admission."


class FakeUtcClock:
    """可控 UTC 时钟：``__call__`` 返回当前固定时间，``advance`` 确定性推进。

    满足生产 ``clock`` DI 契约（返回带时区 UTC datetime 的 callable）。
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
        self.call_count = 0

    def __call__(self) -> datetime:
        self.call_count += 1
        return self._now

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float) -> datetime:
        """确定性推进时钟（不使用真实 sleep）并返回新时间。"""
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


@dataclass(slots=True)
class SentPrivateMessage:
    """一次 ``send_private_msg`` 成功投递的记录。"""

    user_id: int
    message: Any


class FakeAdmissionBot:
    """最小 OneBot Bot 替身：只实现通知投递所需的 ``send_private_msg``。

    ``fail=True`` 时每次投递抛错（模拟单 Bot 发送失败，不得冒泡）。
    """

    def __init__(self, bot_id: str, *, fail: bool = False) -> None:
        self.self_id = bot_id
        self.fail = fail
        self.attempts = 0
        self.sent: list[SentPrivateMessage] = []

    async def send_private_msg(self, *, user_id: int, message: Any) -> None:
        self.attempts += 1
        if self.fail:
            msg = f"bot {self.self_id} send path unavailable"
            raise RuntimeError(msg)
        self.sent.append(SentPrivateMessage(user_id=user_id, message=message))


class MutableBotsProvider:
    """可变的在线 Bot 提供者：模拟 Bot 上线/下线与枚举形态差异。

    ``as_mapping=True`` 时返回 ``{self_id: bot}`` Mapping（NoneBot
    ``get_bots()`` 形态）；否则返回 list（Iterable 形态）。枚举顺序固定为
    注入顺序。
    """

    def __init__(
        self,
        bots: Iterable[FakeAdmissionBot] = (),
        *,
        as_mapping: bool = False,
    ) -> None:
        self._bots: list[FakeAdmissionBot] = list(bots)
        self.as_mapping = as_mapping

    def set_bots(self, *bots: FakeAdmissionBot) -> None:
        self._bots = list(bots)

    def __call__(self) -> Mapping[str, FakeAdmissionBot] | list[FakeAdmissionBot]:
        if self.as_mapping:
            return {bot.self_id: bot for bot in self._bots}
        return list(self._bots)


class MutableSuperusersProvider:
    """可变的 SUPERUSER 提供者：返回 Iterable（允许混入非法值验证照收）。"""

    def __init__(self, values: Iterable[object] = ()) -> None:
        self._values: list[object] = list(values)

    def set_values(self, *values: object) -> None:
        self._values = list(values)

    def __call__(self) -> list[object]:
        return list(self._values)


def build_runtime_kwargs(
    *,
    clock: FakeUtcClock | None = None,
    bots_provider: MutableBotsProvider | None = None,
    superusers_provider: MutableSuperusersProvider | None = None,
) -> dict[str, object]:
    """按需组装 ``_AdmissionRuntime`` 的可选内部 DI 关键字。

    未提供的键不出现在结果中：生产必须对缺省键使用真实默认值，而不是要求
    调用方显式注入。
    """
    kwargs: dict[str, object] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if bots_provider is not None:
        kwargs["online_bots_provider"] = bots_provider
    if superusers_provider is not None:
        kwargs["superusers_provider"] = superusers_provider
    return kwargs


class AdmissionLogCapture:
    """按 ``extra.event`` 前缀收集的准入结构化日志 record。"""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def _sink(self, message: Any) -> None:
        record = message.record
        event = record["extra"].get("event")
        if isinstance(event, str) and event.startswith(_EVENT_PREFIX):
            self.records.append(record)

    def by_event(self, event_name: str) -> list[Any]:
        return [
            record
            for record in self.records
            if record["extra"].get("event") == event_name
        ]

    def event_names(self) -> list[str]:
        return [str(record["extra"].get("event")) for record in self.records]

    def assert_no_events(self) -> None:
        assert self.records == [], (
            f"预期零准入结构化日志，实际出现: {self.event_names()}"
        )


@contextmanager
def capture_admission_logs() -> Iterator[AdmissionLogCapture]:
    """在 NoneBot/Loguru 真实 logger 上挂载捕获 sink（退出时移除）。

    生产必须用真实结构化日志（``logger.bind(event=...)``）；测试不注入
    logger，也不强迫生产接受 logger DI。
    """
    from nonebot import logger

    capture = AdmissionLogCapture()
    sink_id = logger.add(capture._sink, level="TRACE")
    try:
        yield capture
    finally:
        logger.remove(sink_id)


def assert_rfc3339_utc(value: object, *, field_name: str) -> datetime:
    """断言值是 UTC RFC 3339 字符串并返回解析后的 datetime。"""
    assert isinstance(value, str), f"{field_name} 必须是字符串: {value!r}"
    assert RFC3339_UTC_PATTERN.fullmatch(value), (
        f"{field_name} 不是 UTC RFC 3339 形态: {value!r}"
    )
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0), f"{field_name} 必须是 UTC"
    return parsed


def assert_status_exact_shape(body: Mapping[str, Any]) -> None:
    """断言 status 响应顶层与 telemetry 子对象的精确键集。"""
    assert set(body) == EXPECTED_STATUS_KEYS, (
        f"status 顶层键集不精确: 多余={sorted(set(body) - EXPECTED_STATUS_KEYS)}, "
        f"缺失={sorted(EXPECTED_STATUS_KEYS - set(body))}"
    )
    telemetry = body["telemetry"]
    assert isinstance(telemetry, Mapping)
    assert set(telemetry) == EXPECTED_TELEMETRY_KEYS, (
        "telemetry 键集不精确: "
        f"多余={sorted(set(telemetry) - EXPECTED_TELEMETRY_KEYS)}, "
        f"缺失={sorted(EXPECTED_TELEMETRY_KEYS - set(telemetry))}"
    )


def assert_telemetry_closed_maps(
    telemetry: Mapping[str, Any],
    *,
    total: int | None = None,
) -> None:
    """断言四张低基数 map 始终精确预初始化封闭键集（零值也在）。"""
    by_reason = telemetry["by_reason_code"]
    by_intent = telemetry["by_intent"]
    by_family = telemetry["attribution_failures_by_event_family"]
    by_problem = telemetry["runtime_problem_occurrences"]
    assert set(by_reason) == CLOSED_REASON_CODES, (
        f"by_reason_code 键集不封闭: {sorted(by_reason)}"
    )
    assert set(by_intent) == CLOSED_INTENTS, (
        f"by_intent 键集不封闭: {sorted(by_intent)}"
    )
    assert set(by_family) == CLOSED_EVENT_FAMILIES, (
        f"attribution_failures_by_event_family 键集不封闭: {sorted(by_family)}"
    )
    assert set(by_problem) == CLOSED_PROBLEM_CODES, (
        f"runtime_problem_occurrences 键集不封闭: {sorted(by_problem)}"
    )
    for mapping in (by_reason, by_intent, by_family, by_problem):
        for key, value in mapping.items():
            assert isinstance(value, int) and not isinstance(value, bool), (
                f"telemetry 计数必须是 int: {key}={value!r}"
            )
            assert value >= 0, f"telemetry 计数不得为负: {key}={value!r}"
    adjudications_total = telemetry["adjudications_total"]
    assert isinstance(adjudications_total, int)
    assert adjudications_total >= 0
    if total is not None:
        assert adjudications_total == total, (
            f"adjudications_total={adjudications_total} 与逐事件累计 {total} 不符"
        )
        assert sum(by_reason.values()) == total, "by_reason_code 合计与 total 不符"
        assert sum(by_intent.values()) == total, "by_intent 合计与 total 不符"


def assert_runtime_extra_whitelist(
    record: Any,
    *,
    whitelist: frozenset[str],
) -> dict[str, Any]:
    """断言一条日志的 extra 键全部在白名单内且必含 event；返回 extra。

    NoneBot 日志集成会向所有记录注入 ``nonebot_*`` 基础设施键（如
    ``nonebot_log_level``），不属于业务内容，白名单检查忽略它们。
    """
    extra = dict(record["extra"])
    leaked_keys = {
        key
        for key in set(extra) - whitelist
        if not key.startswith("nonebot_")
    }
    assert leaked_keys == set(), (
        f"日志 extra 出现白名单外的键: {sorted(leaked_keys)} (event={extra.get('event')})"
    )
    assert "event" in extra
    return extra


def business_extra(record: Any) -> dict[str, Any]:
    """提取业务 extra 键（剔除 NoneBot 日志集成注入的 ``nonebot_*`` 基础设施键）。

    用于键集精确断言：框架注入键不属于业务内容，不参与白名单/精确集比较。
    """
    return {
        key: value
        for key, value in dict(record["extra"]).items()
        if not key.startswith("nonebot_")
    }


def deliver_snapshot_direct(
    runtime: Any,
    *,
    revision: int,
    policy: Mapping[str, Any],
    updated_at: datetime,
) -> None:
    """绕过 ConfigManager 修订过滤，直接向准入运行时快照 listener 接缝投递。

    真实 ``ConfigManager._accept_stored_snapshot`` 只向 listener 转发严格更高
    revision，乱序/重复快照在共享配置层即被丢弃，永远到不了准入运行时；因此
    运行时自身的乱序观测守卫只能经其快照 listener 接缝（``_on_snapshot``，
    runtime_support 既以此接缝摘除 listener）直接驱动。构造的快照与生产同形：
    permissive value schema + revision + updated_at。
    """
    from komari_bot.plugins.config_manager.manager import ConfigSnapshot
    from tests.group_admission.runtime_support import AdmissionValueSchema

    snapshot = ConfigSnapshot(
        value=AdmissionValueSchema(policy=dict(policy)),
        revision=revision,
        updated_at=updated_at,
    )
    runtime._on_snapshot(snapshot)


def card_texts(bots: Iterable[FakeAdmissionBot]) -> list[tuple[int, str]]:
    """收集全部 fake Bot 成功投递的 ``(user_id, 文本)`` 清单。"""
    collected: list[tuple[int, str]] = []
    for bot in bots:
        collected.extend(
            (sent.user_id, str(sent.message)) for sent in bot.sent
        )
    return collected


def _parse_card_fields(card_text: str) -> dict[str, str]:
    """把 SUPERUSER 故障/恢复私聊卡解析为 ``{key: value}`` 字段映射（全部字符串）。

    只解析 ``key: value`` 形态的行；首行固定合并文案
    （``COMBINED_CARD_BANNER``）与空行跳过。遇到非 ``key: value`` 行即断言
    失败，防止未经解析的正文混入字段集。
    """
    fields: dict[str, str] = {}
    for raw_line in card_text.splitlines():
        line = raw_line.strip()
        if not line or line == COMBINED_CARD_BANNER:
            continue
        key, separator, value = line.partition(": ")
        assert separator, f"卡文本含非「key: value」行: {line!r}"
        fields[key] = value
    return fields


def assert_exact_card(
    card_text: str,
    *,
    expected: Mapping[str, object],
    resolved: bool = False,
    combined: bool = False,
) -> None:
    """断言一张 SUPERUSER 故障/恢复卡满足 TSK-217 §7 冻结白名单与期望值。

    - 卡字段集合必须严格等于 ``FAULT_CARD_FIELDS``（``resolved=True`` 时
      ``FAULT_RESOLVED_CARD_FIELDS``）；出现任何白名单外字段
      （含禁止字段 ``occurrence_count`` / ``FAULT_CARD_FORBIDDEN_KEY``）即失败；
    - ``expected`` 的全部字段必须出现，且渲染值（字符串）逐字段相等；
    - ``combined=True`` 时首行必须为 ``COMBINED_CARD_BANNER`` 合并文案。
    """
    allowed = FAULT_RESOLVED_CARD_FIELDS if resolved else FAULT_CARD_FIELDS
    lines = card_text.splitlines()
    if combined:
        assert lines and lines[0].strip() == COMBINED_CARD_BANNER, (
            f"合并卡必须首行为 {COMBINED_CARD_BANNER!r}"
        )
    fields = _parse_card_fields(card_text)
    actual = set(fields)
    missing = allowed - actual
    extra = actual - allowed
    assert not missing and not extra, (
        f"卡字段集不严格等于白名单: "
        f"缺失={sorted(missing)}, 多余={sorted(extra)}"
    )
    assert FAULT_CARD_FORBIDDEN_KEY not in fields, (
        f"卡不得包含禁止字段 {FAULT_CARD_FORBIDDEN_KEY!r}: {card_text!r}"
    )
    for key, expected_value in expected.items():
        assert str(fields[key]) == str(expected_value), (
            f"字段 {key}={fields[key]!r} 不等于预期 {expected_value!r}"
        )


def runtime_log_projection(record: Any) -> dict[str, Any]:
    """把日志 record 投影为可扫描的安全字典（message + extra）。"""
    return {
        "message": str(record["message"]),
        "extra": {str(key): value for key, value in record["extra"].items()},
    }


@dataclass(frozen=True, slots=True)
class WindowExpectation:
    """归属窗口时间边界预期（299/300 秒冻结语义的文档化）。"""

    window_seconds: int = ATTRIBUTION_WINDOW_SECONDS
    last_in_window_offset: int = field(default=ATTRIBUTION_WINDOW_SECONDS - 1)
    first_of_next_window_offset: int = field(default=ATTRIBUTION_WINDOW_SECONDS)
