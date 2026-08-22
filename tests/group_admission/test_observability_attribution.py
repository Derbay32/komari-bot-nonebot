"""TSK-223 阶段 B：``group_attribution_unavailable`` 5 分钟聚合窗口。

验收目标（TSK-217 §4 冻结，全程假时钟、无真实 sleep）：

- 去重键只有 ``(group_attribution_unavailable, event_family)``；
- 窗口首次出现：立即 1 条 ``attribution_unavailable`` WARNING 并排队 1 张
  卡；窗口内后续只累计（无日志、无新卡）；
- 窗口结束后的首次 ``process_observability`` 输出上一窗口安全摘要
  （``suppressed_count`` 精确）；同一窗口至多 1 条首次 + 1 条结束摘要；
  下一窗口重新首条；不同 family 相互独立；非法 family 归一 ``unknown``；
- 边界：窗口内最后一秒（299）仍属本窗口；300 秒整进入下一窗口；
- telemetry 始终累计（不受节流影响）；
- 卡只允许 reason_code / event_family / occurrence_count /
  window_started_at 语义（固定标题不算字段），无 IDs / 正文。
"""

from __future__ import annotations

import re

import pytest

from tests.group_admission.management_support import (
    READER_TOKEN,
    STATUS_PATH,
    asgi_client,
    atomic_policy,
    auth_headers,
    prepare_control_plane,
)
from tests.group_admission.observability_support import (
    ATTRIBUTION_EXTRA_WHITELIST,
    ATTRIBUTION_WINDOW_SECONDS,
    EVENT_ATTRIBUTION_UNAVAILABLE,
    FakeAdmissionBot,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    assert_rfc3339_utc,
    assert_runtime_extra_whitelist,
    build_runtime_kwargs,
    capture_admission_logs,
    card_texts,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

_SUPERUSER_ID = 42

#: 非锚定的 UTC RFC 3339 搜索式（供字段行内精确提取时间戳；
#: ``RFC3339_UTC_PATTERN`` 带 ``^...$`` 锚点，仅用于整行 fullmatch）。
_RFC3339_UTC_SEARCH = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)"
)


def _window_kwargs(
    clock: FakeUtcClock,
    bot: FakeAdmissionBot,
) -> dict[str, object]:
    return build_runtime_kwargs(
        clock=clock,
        bots_provider=MutableBotsProvider((bot,)),
        superusers_provider=MutableSuperusersProvider((_SUPERUSER_ID,)),
    )


def _assert_window_started_at(text: str, expected: object) -> None:
    """卡中的窗口开始时间必须是预期时刻的 UTC RFC 3339 形态。

    只解析固定 ``window_started_at`` 字段所在行，不扫描整张卡中任意 UTC 时间
    （避免误匹配其他时间戳字段）。
    """
    target_line = next(
        (line for line in text.splitlines() if "window_started_at" in line), None
    )
    assert target_line is not None, f"卡缺少 window_started_at 字段行: {text!r}"
    match = _RFC3339_UTC_SEARCH.search(target_line)
    assert match is not None, (
        f"window_started_at 字段行缺少 UTC RFC 3339 时间: {target_line!r}"
    )
    parsed = assert_rfc3339_utc(match.group(0), field_name="window_started_at")
    assert parsed == expected, (
        f"窗口开始时间与首次发生时刻不符: {match.group(0)} vs {expected}"
    )


async def test_first_attribution_failure_logs_and_queues_exactly_one_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首条立即日志 + 排队恰一卡；窗口内后续只累计；摘要精确。（anchor）"""
    clock = FakeUtcClock()
    t0 = clock.now
    bot = FakeAdmissionBot("bot-attribution")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_window_kwargs(clock, bot)
    )

    with capture_admission_logs() as capture:
        runtime.adjudicate(["not-a-group"], event_family="message")
        first_records = capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)
        assert len(first_records) == 1, capture.event_names()
        extra = assert_runtime_extra_whitelist(
            first_records[0], whitelist=ATTRIBUTION_EXTRA_WHITELIST
        )
        assert extra["event_family"] == "message"
        assert extra["window_seconds"] == ATTRIBUTION_WINDOW_SECONDS
        assert extra["status"] == "ready"

        # 日志立即产生，但通知在 process 前不投递
        assert bot.sent == []

        # 窗口内后续只累计：无新日志、无新卡
        clock.advance(seconds=100)
        runtime.adjudicate(["bad"], event_family="message")
        clock.advance(seconds=100)
        runtime.adjudicate([None], event_family="message")
        clock.advance(seconds=99)  # t0+299：窗口内最后一秒
        runtime.adjudicate(["bad"], event_family="message")
        assert len(capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)) == 1
        assert bot.sent == []

        # 窗口未结束的 process：投递已排队的首卡（不产生摘要）
        await runtime.process_observability()
        assert len(capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)) == 1
        assert len(bot.sent) == 1, "窗口首卡必须在首次 process 投递"

        # 窗口结束后的首次 process：恰一条安全摘要
        clock.advance(seconds=1)  # t0+300
        await runtime.process_observability()
        records = capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)
        assert len(records) == 2, capture.event_names()
        summary_extra = assert_runtime_extra_whitelist(
            records[1], whitelist=ATTRIBUTION_EXTRA_WHITELIST
        )
        assert summary_extra["suppressed_count"] == 3, (
            "摘要必须精确报告窗口内被抑制的后续次数"
        )
        assert summary_extra["event_family"] == "message"

    # 卡片内容：只含安全字段（reason/family/次数/窗口开始时间）
    cards = card_texts((bot,))
    assert len(cards) == 1
    user_id, text = cards[0]
    assert user_id == _SUPERUSER_ID
    assert "group_attribution_unavailable" in text
    assert "message" in text
    assert "4" in text, "卡必须携带窗口内观察总次数"
    _assert_window_started_at(text, t0)

    # telemetry 始终累计（不受节流影响）
    async with asgi_client(app) as client:
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))
    telemetry = response.json()["telemetry"]
    assert telemetry["attribution_failures_by_event_family"]["message"] == 4
    assert telemetry["by_reason_code"]["group_attribution_unavailable"] == 4


async def test_window_boundary_299_same_window_300_new_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """299 秒仍是同一窗口（只累计）；300 秒整开启新窗口（重新首条）。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-boundary")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_window_kwargs(clock, bot)
    )

    with capture_admission_logs() as capture:
        runtime.adjudicate(["bad"], event_family="notice")

        clock.advance(seconds=ATTRIBUTION_WINDOW_SECONDS - 1)
        runtime.adjudicate(["bad"], event_family="notice")
        assert len(capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)) == 1, (
            "299 秒仍属同一窗口，不得重新首条"
        )

        clock.advance(seconds=1)  # 恰好 300 秒
        runtime.adjudicate(["bad"], event_family="notice")
        records = capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)
        assert len(records) == 2, "300 秒整必须开启新窗口并重新首条"

    async with asgi_client(app) as client:
        telemetry = (
            (await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN)))
            .json()["telemetry"]
        )
    assert telemetry["attribution_failures_by_event_family"]["notice"] == 3


async def test_event_families_are_independent_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同 family 独立首条/独立计数；非法 family 归一 unknown 后独立。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-families")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_window_kwargs(clock, bot)
    )

    with capture_admission_logs() as capture:
        runtime.adjudicate(["bad"], event_family="message")
        runtime.adjudicate(["bad"], event_family="notice")
        runtime.adjudicate(["bad"], event_family="MeSsAgE-not-valid")
        runtime.adjudicate(["bad"], event_family="request")

        records = capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)
        assert len(records) == 4, "每个 family 都应独立首条"
        families = [record["extra"]["event_family"] for record in records]
        assert families == ["message", "notice", "unknown", "request"], (
            f"非法 family 必须归一 unknown 且各自独立: {families}"
        )

    async with asgi_client(app) as client:
        telemetry = (
            (await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN)))
            .json()["telemetry"]
        )
    assert telemetry["attribution_failures_by_event_family"] == {
        "message": 1,
        "notice": 1,
        "request": 1,
        "unknown": 1,
    }


async def test_attribution_card_content_stays_within_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """卡只承载安全字段：恶意归因内容/群号/用户号绝不进入卡或日志。"""
    from tests.group_admission.sensitive_canary import build_extended_canary_bundle

    bundle = build_extended_canary_bundle()
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-safe-card")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    _app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_window_kwargs(clock, bot)
    )

    with capture_admission_logs() as capture:
        # 恶意归因载荷：URL / 群号 / 用户号 / 消息 ID 全部非法
        runtime.adjudicate(
            [
                "https://canary.example/exfil?key=9f3a7c",
                779001,
                889002,
                "msg-CANARY-9f2e77",
            ],
            event_family="message",
        )
        await runtime.process_observability()

    cards = card_texts((bot,))
    assert len(cards) == 1
    _user_id, text = cards[0]
    bundle.assert_no_leaks(text, context="归属异常卡")
    assert "779001" not in text
    assert "889002" not in text

    for record in capture.records:
        bundle.assert_no_leaks(
            {
                "message": str(record["message"]),
                "extra": dict(record["extra"]),
            },
            context="归属异常日志",
        )


async def test_post_close_invalid_attribution_telemetry_only_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close 后迟到的非法归属裁决：基础遥测照常 +1，观测侧全部 no-op。

    关闭运行时后再调用 ``adjudicate(["bad"], event_family="message")``：
    ``adjudications_total`` / ``group_attribution_unavailable`` / ``business``
    / family 基础遥测仍恰 +1，但不得再开归属窗口、不得产生任何
    ``group_admission.*`` 结构化日志、不得排队/投递通知卡，且
    ``process_observability`` 整体 no-op。只经日志 capture 与 Bot 可观察效果
    断言，不触碰 pending 内部列表。
    """
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-postclose-attribution")
    storage = AdmissionStorageFake(
        stored_policy(1, atomic_policy("blacklist", []))
    )
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_window_kwargs(clock, bot)
    )

    await runtime.close()

    with capture_admission_logs() as capture:
        runtime.adjudicate(["bad"], event_family="message")
        await runtime.process_observability()
        capture.assert_no_events()

    assert bot.sent == []
    assert bot.attempts == 0

    async with asgi_client(app) as client:
        response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
    assert response.status_code == 200, response.text
    telemetry = response.json()["telemetry"]
    assert telemetry["adjudications_total"] == 1
    assert telemetry["by_reason_code"]["group_attribution_unavailable"] == 1
    assert telemetry["by_intent"]["business"] == 1
    assert telemetry["attribution_failures_by_event_family"]["message"] == 1
