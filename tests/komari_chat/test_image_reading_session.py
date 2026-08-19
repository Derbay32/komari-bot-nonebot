"""TSK-195/ADR-0010 任务级图片理解会话（delegated）单元测试。

覆盖（测试即规范）：

- 稳定引用/源归属/原来源序号在构建时固定，引用消息图片在前、当前消息
  图片在后；失败不删除、不压缩、不重编号；
- 构建即零预下载：只有首次 ``read(index)`` 才懒下载并交由视觉模型描述；
- 同 index 重复读取：成功复用同一描述、失败复用同一结构化失败；
  并发重复读取同 index 单飞（只一次下载 + 一次视觉调用）；
- 非法/越界 index 零外部调用并返回结构化失败；
- 会话任务间完全隔离；全部失败状态提供安全摘要（无 URL/base64/正文）。

本文件通过公开 seam 驱动真实 ``ImageReadingSession``：monkeypatch 模块级
``ImageDownloadSession``（安全下载器工厂）与 ``read_images``（视觉读取），
不断言纯私有 helper 行为、不额外为测试添加生产后门。
"""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

image_reading_session_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
image_downloader_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_downloader"
)

ImageReadingSession = image_reading_session_module.ImageReadingSession

_RAW_URLS = [
    "https://example.com/secret/path/quoted-0.png?token=abc#frag",
    "https://example.com/secret/path/quoted-1.png?token=abc#frag",
    "https://example.com/secret/path/current-0.png?token=abc#frag",
]


def _policy(**overrides: object) -> image_downloader_module.ImageDownloadPolicy:
    values: dict[str, object] = {
        "max_images": 4,
        "max_image_bytes": 8 * 1024 * 1024,
        "max_total_bytes": 20 * 1024 * 1024,
        "max_pixels": 40_000_000,
        "concurrency": 2,
        "connect_timeout_seconds": 5.0,
        "read_timeout_seconds": 30.0,
        "total_timeout_seconds": 45.0,
    }
    values.update(overrides)
    return image_downloader_module.ImageDownloadPolicy(**values)  # type: ignore[arg-type]


class _FakeDownloader:
    """可控下载器：记录调用并返回预设结果（str | None | callable）。"""

    def __init__(self, results: Any = "data:image/png;base64,QQ==") -> None:
        self.results = results
        self.calls: list[str] = []
        self.close_calls = 0

    async def download(self, url: str) -> str | None:
        self.calls.append(url)
        if callable(self.results):
            return self.results()
        return self.results

    async def close(self) -> None:
        self.close_calls += 1


class _SlowDownloader:
    """并发单飞测试用慢速下载器。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, url: str) -> str:
        self.calls.append(url)
        await asyncio.sleep(0.05)
        return "data:image/png;base64,QQ=="

    async def close(self) -> None:
        return None


class _FailSecondDownloader:
    """第 1 次成功、第 2 次失败；用于多 index 记账与部分失败。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, url: str) -> str | None:
        self.calls.append(url)
        return "data:image/png;base64,QQ==" if len(self.calls) == 1 else None

    async def close(self) -> None:
        return None


class _FakeVision:
    """记录视觉调用；可按预设返回成功或失败描述。"""

    def __init__(self, result: str = "一只猫") -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def __call__(self, images: list[str], **kwargs: object) -> list[str]:
        self.calls.append({"images": list(images), **kwargs})
        if self.result.startswith("[图片读取失败:"):
            return [self.result]
        return [f"{self.result}（{len(self.result)}字）"]


def _build_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    quoted: list[str] | None = None,
    current: list[str] | None = None,
    policy: Any = None,
    downloader: Any = None,
    vision: _FakeVision | None = None,
    max_images: int | None = None,
) -> tuple[ImageReadingSession, _FakeVision]:
    fake_downloader = downloader if downloader is not None else _FakeDownloader()
    fake_vision = vision if vision is not None else _FakeVision()
    monkeypatch.setattr(
        image_reading_session_module,
        "ImageDownloadSession",
        lambda _policy: fake_downloader,
    )
    monkeypatch.setattr(
        image_reading_session_module,
        "read_images",
        fake_vision,
    )
    sprite_policy = policy if policy is not None else _policy()
    session = ImageReadingSession.build(
        quoted_sources=quoted or [],
        current_sources=current or [],
        policy=sprite_policy,
        max_images=max_images,
        vision_model="vision-model",
        vision_temperature=0.3,
        vision_max_tokens=1024,
        vision_request_api="chat_completions",
        vision_stream_enabled=False,
        vision_thinking_mode=False,
        vision_reasoning_effort="",
        request_trace_id="trace-session-1",
        collector=None,
    )
    return session, fake_vision


# ── 稳定引用与源归属 ────────────────────────────────────────────────────


def test_build_stable_indexes_quoted_first_preserves_original() -> None:
    """引用消息图片在前、当前消息图片在后；index 稳定且保留来源与原序号。"""
    session = ImageReadingSession.build(
        quoted_sources=[_RAW_URLS[0], _RAW_URLS[1]],
        current_sources=[_RAW_URLS[2]],
        policy=_policy(),
        vision_model="vision-model",
    )

    assert session.total_count == 3
    assert session.quoted_count == 2
    assert session.current_count == 1

    refs = list(session._references)
    assert [(ref.index, ref.origin, ref.original_index) for ref in refs] == [
        (0, "quoted", 0),
        (1, "quoted", 1),
        (2, "current", 0),
    ]
    # 原始 URL 只在引用对象内（会话→安全下载器边界），不与计数/结果耦合
    assert [ref.source for ref in refs] == [
        _RAW_URLS[0],
        _RAW_URLS[1],
        _RAW_URLS[2],
    ]


def test_build_caps_references_at_max_images() -> None:
    """超过 max_images 的来源截断，索引仍从 0 连续且引用在前。"""
    session = ImageReadingSession.build(
        quoted_sources=[_RAW_URLS[0], _RAW_URLS[1]],
        current_sources=[_RAW_URLS[2]],
        policy=_policy(max_images=2),
        vision_model="vision-model",
    )

    assert session.total_count == 2
    assert session.quoted_count == 2
    assert session.current_count == 0
    assert [(ref.index, ref.origin, ref.original_index) for ref in session._references] == [
        (0, "quoted", 0),
        (1, "quoted", 1),
    ]


# ── 零预下载 / 首次读取 / 缓存 ──────────────────────────────────────────


def test_construction_does_not_download_or_call_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """construct 即零预下载：不触达下载器也不调用视觉模型。"""

    def _raise(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("构建会话时不得下载或调用视觉模型")

    downloader = _FakeDownloader(None)
    downloader.download = _raise  # type: ignore[method-assign]
    session, vision = _build_session(
        monkeypatch,
        quoted=[_RAW_URLS[0], _RAW_URLS[1]],
        current=[_RAW_URLS[2]],
        downloader=downloader,
    )
    vision._boom = _raise  # type: ignore[attr-defined]

    assert session.total_count == 3


def test_first_read_lazy_downloads_and_calls_vision_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次 read(index) 才懒下载一次并调用视觉一次。"""
    downloader = _FakeDownloader("data:image/png;base64,MQ==")
    session, vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[2]],
        downloader=downloader,
    )

    result = asyncio.run(session.read(0))

    assert result.status == "success"
    assert result.description == "一只猫（3字）"
    assert downloader.calls == [_RAW_URLS[2]]
    assert len(vision.calls) == 1
    sent_image = vision.calls[0]["images"]
    assert sent_image == ["data:image/png;base64,MQ=="], (
        "视觉模型只收到下载后的 data URI，不收到原始 URL"
    )


def test_repeated_success_read_reuses_same_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 index 重复读取成功时复用同一描述，不重复下载/视觉。"""
    downloader = _FakeDownloader("data:image/png;base64,MQ==")
    session, vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[2]],
        downloader=downloader,
    )

    first = asyncio.run(session.read(0))
    second = asyncio.run(session.read(0))

    assert first is second
    assert first.status == "success"
    assert second.description == first.description
    assert downloader.calls == [_RAW_URLS[2]]
    assert len(vision.calls) == 1


def test_repeated_failure_read_reuses_same_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 index 重复读取失败时复用同一结构化失败，不重复下载/视觉。"""
    downloader = _FakeDownloader(None)
    session, vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[2]],
        downloader=downloader,
    )

    first = asyncio.run(session.read(0))
    second = asyncio.run(session.read(0))

    assert first is second
    assert first.status == "failure"
    assert first.error_type == "image_unavailable"
    assert first.failure_message == second.failure_message
    assert "图片读取失败" in first.failure_message
    assert downloader.calls == [_RAW_URLS[2]]
    assert len(vision.calls) == 0, "下载失败不得触发视觉调用"


def test_concurrent_same_index_reads_are_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发重复读取同 index 单飞：只一次下载与一次视觉调用。"""
    downloader = _SlowDownloader()
    session, vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[2]],
        downloader=downloader,
    )

    async def _read_many() -> list[Any]:
        return list(
            await asyncio.gather(*(session.read(0) for _ in range(8)))
        )

    results = asyncio.run(_read_many())

    assert all(result.status == "success" for result in results)
    assert len({id(result) for result in results}) == 1, "并发读必须共享同一结果对象"
    assert downloader.calls == [_RAW_URLS[2]], "并发重复读取只下载一次"
    assert len(vision.calls) == 1, "并发重复读取只调用一次视觉模型"


def test_invalid_and_out_of_range_index_zero_external_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法/越界 index 零外部调用并返回结构化失败。"""
    session, vision = _build_session(monkeypatch, current=[_RAW_URLS[2]])

    results = [
        asyncio.run(session.read(-1)),
        asyncio.run(session.read(99)),
    ]

    for result in results:
        assert result.status == "invalid_index"
        assert result.error_type == "invalid_index"
        assert "超出范围" in result.failure_message
    assert len(vision.calls) == 0


# ── 全部失败状态 / 任务隔离 / 安全摘要 ─────────────────────────────────


def test_partial_failure_keeps_other_indices_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中间 index 失败只缓存该 index 失败，其他 index 仍可读。"""
    downloader = _FailSecondDownloader()
    session, vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[0], _RAW_URLS[1]],
        downloader=downloader,
    )

    first = asyncio.run(session.read(0))
    second = asyncio.run(session.read(1))

    assert first.status == "success"
    assert second.status == "failure"
    # 再次读取失败 index 复用同一失败；index 0 仍可读
    assert asyncio.run(session.read(1)) is second
    assert asyncio.run(session.read(0)).description == first.description
    assert downloader.calls == [_RAW_URLS[0], _RAW_URLS[1]]
    assert len(vision.calls) == 1


def test_all_images_unavailable_state_and_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部可用引用均失败时产生 all_images_unavailable 领域状态与安全摘要。"""
    session, _vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[0], _RAW_URLS[1]],
        downloader=_FakeDownloader(None),
    )

    assert session.all_images_unavailable() is False
    asyncio.run(session.read(0))
    assert session.all_images_unavailable() is False, "只读一张时不算全部失败"
    second = asyncio.run(session.read(1))
    assert second.status == "failure"
    assert session.all_images_unavailable() is True

    summary = session.failure_summary()
    assert summary.mode == "delegated"
    assert summary.all_images_unavailable is True
    assert summary.total_images == 2
    assert summary.attempted_images == 2
    assert summary.failed_images == 2
    assert summary.error_types == ("image_unavailable",)
    assert summary.stages == ("download",)
    # 安全摘要不得携带 URL / base64 / 正文
    rendered = str(summary)
    assert "example.com" not in rendered
    assert _RAW_URLS[0] not in rendered and _RAW_URLS[1] not in rendered


def test_not_all_failed_when_some_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分成功则 all_images_unavailable 保持 False。"""
    session, _vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[0], _RAW_URLS[1]],
        downloader=_FailSecondDownloader(),
    )

    asyncio.run(session.read(0))
    second = asyncio.run(session.read(1))
    assert second.status == "failure"
    assert session.all_images_unavailable() is False
    summary = session.failure_summary()
    assert summary.failed_images == 1
    assert summary.attempted_images == 2
    assert summary.all_images_unavailable is False


def test_sessions_are_isolated_between_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同任务的会话实例完全隔离：缓存、下载器与摘要互不影响。"""
    first_downloader = _FakeDownloader("data:image/png;base64,MQ==")
    second_downloader = _FakeDownloader(None)
    monkeypatch.setattr(
        image_reading_session_module,
        "read_images",
        _FakeVision(),
    )
    monkeypatch.setattr(
        image_reading_session_module,
        "ImageDownloadSession",
        lambda _policy: first_downloader,
    )
    session_a = ImageReadingSession.build(
        quoted_sources=[],
        current_sources=[_RAW_URLS[0]],
        policy=_policy(),
        vision_model="vision-model",
    )
    monkeypatch.setattr(
        image_reading_session_module,
        "ImageDownloadSession",
        lambda _policy: second_downloader,
    )
    session_b = ImageReadingSession.build(
        quoted_sources=[],
        current_sources=[_RAW_URLS[1]],
        policy=_policy(),
        vision_model="vision-model",
    )

    result_a = asyncio.run(session_a.read(0))
    result_b = asyncio.run(session_b.read(0))

    assert session_a is not session_b
    assert result_a.status == "success"
    assert result_b.status == "failure"
    assert first_downloader.calls == [_RAW_URLS[0]]
    assert second_downloader.calls == [_RAW_URLS[1]]
    assert session_a.all_images_unavailable() is False
    assert session_b.all_images_unavailable() is True


def test_close_releases_download_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话关闭时释放下载会话连接。"""
    downloader = _FakeDownloader(None)
    session, _vision = _build_session(
        monkeypatch,
        current=[_RAW_URLS[0]],
        downloader=downloader,
    )

    asyncio.run(session.close())

    assert downloader.close_calls == 1
