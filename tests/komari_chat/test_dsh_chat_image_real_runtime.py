"""Real installed-DSH native image transport acceptance."""

from __future__ import annotations

import asyncio
import base64
import io
import os
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image

from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext
from tests.komari_agent.dsh_test_support import (
    MAIN_HOST_SCRIPT,
    ToolCallResponse,
    running_dsh,
)
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
    recall_response,
)

service_module = import_module(
    "komari_bot.plugins.komari_chat.services.dsh_chat_service"
)
native_module = import_module(
    "komari_bot.plugins.komari_chat.services.native_image"
)
failure_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_failure"
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_QUOTED_URL = "https://example.invalid/private/quoted-source?token=quoted-secret"
_CURRENT_URL = "https://example.invalid/private/current-source?token=current-secret"


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        bot_nickname="小鞠",
        knowledge_enabled=False,
        knowledge_limit=3,
        agent_max_rounds=4,
        agent_max_tool_calls_per_round=2,
        agent_max_total_tool_calls=4,
        vision_image_download_max_count=8,
        vision_image_download_max_bytes=16 * 1024 * 1024,
        vision_image_download_total_max_bytes=32 * 1024 * 1024,
        vision_image_download_max_pixels=100_000_000,
        vision_image_download_concurrency=2,
        vision_image_download_connect_timeout_seconds=5.0,
        vision_image_download_read_timeout_seconds=30.0,
        vision_image_download_total_timeout_seconds=45.0,
    )


def _message(index: int, content: str) -> MessageSchema:
    return MessageSchema(
        user_id=f"u-{index}",
        user_nickname=f"用户{index}",
        group_id="g-1",
        content=content,
        timestamp=float(index),
        message_id=f"m-{index}",
    )


async def _favorability(_uid: str) -> object:
    return SimpleNamespace(
        favorability=7,
        stage_index=1,
        stage_name="熟悉",
        stage_prompt="关系逐渐熟悉。",
    )


async def _empty_search(**_kwargs: object) -> list[object]:
    return []


async def _empty_uid(_uid: str) -> list[object]:
    return []


def _group_bindings(_group_id: str) -> dict[str, str]:
    return {}


def _source_image(
    fmt: str,
    *,
    size: tuple[int, int],
    color: tuple[int, int, int],
    noise: bool = False,
) -> bytes:
    buffer = io.BytesIO()
    if noise:
        width, height = size
        image = Image.frombytes("RGB", size, os.urandom(width * height * 3))
    else:
        image = Image.new("RGB", size, color)
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def _data_uri(data: bytes, fmt: str) -> str:
    media = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}[fmt]
    return f"data:{media};base64,{base64.b64encode(data).decode('ascii')}"


class _StaticDownloader:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def download(self, url: str) -> str | None:
        self.calls.append(url)
        return self.responses.get(url)

    async def close(self) -> None:
        return None


def _image_urls(request: Any) -> list[str]:
    urls: list[str] = []
    for message in request.body.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url")
            if isinstance(image, dict) and isinstance(image.get("url"), str):
                urls.append(image["url"])
    return urls


async def test_real_dsh_native_images_use_bounded_chunks_and_source_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A >512KiB quoted image plus a current JPEG reaches the real model route."""
    quoted = _source_image("PNG", size=(900, 700), color=(0, 0, 0), noise=True)
    current = _source_image("JPEG", size=(320, 180), color=(10, 120, 220))
    assert len(quoted) > 512 * 1024
    assert len(current) > 0
    downloader = _StaticDownloader(
        {
            _QUOTED_URL: _data_uri(quoted, "PNG"),
            _CURRENT_URL: _data_uri(current, "JPEG"),
        }
    )
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )

    responses = (
        ToolCallResponse("final_response", {"content": "原生图片回复"}),
        ToolCallResponse(
            "record_favorability_delta",
            {"delta": 1, "reason": "看到了图片"},
        ),
    )
    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
        input_modalities=("text", "image"),
    ) as (host, server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        quote = ReplyContext(
            source_side="user",
            message_id="quoted-1",
            user_id="u-2",
            user_nickname="引用用户",
            text="引用的图片",
            image_sources=(_QUOTED_URL,),
            image_count=1,
            has_visible_image=True,
        )
        result = await service.generate(
            message=_message(0, "当前图片"),
            recent_messages=[],
            reply_context=quote,
            recall_session=fake_recall_session(
                FakeHindsightMemory(response=recall_response())
            ),
            request_trace_id="chat-native-real-1",
            config=_config(),
            image_urls=[_CURRENT_URL],
        )

    assert result.content == "原生图片回复"
    assert set(downloader.calls) == {_QUOTED_URL, _CURRENT_URL}
    assert len(server.requests) == 2
    first = server.requests[0]
    task_texts = [text for text in first.user_texts if text.startswith('<komari_chat_task schema="1">')]
    assert len(task_texts) == 1
    task_text = task_texts[0]
    assert "<current_message>" in task_text
    assert "当前图片" in task_text
    assert "引用用户" in task_text
    image_urls = _image_urls(first)
    assert len(image_urls) == 2
    # Official normalization may re-encode, but quoted-then-current order and
    # real raster dimensions survive.
    decoded: list[Image.Image] = []
    for image_url in image_urls:
        assert image_url.startswith("data:image/")
        _header, payload = image_url.split(",", 1)
        decoded.append(Image.open(io.BytesIO(base64.b64decode(payload, validate=True))))
    assert decoded[0].size == (900, 700)
    assert decoded[1].size == (320, 180)
    for request in server.requests:
        rendered = repr(request.body)
        assert _QUOTED_URL not in rendered
        assert _CURRENT_URL not in rendered
        assert "quoted-secret" not in rendered
        assert "current-secret" not in rendered
    for image_url in image_urls:
        assert _QUOTED_URL not in image_url
        assert _CURRENT_URL not in image_url
    profile_root = tmp_path / "data"
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in profile_root.rglob("*")
        if path.is_file()
    )
    assert _QUOTED_URL not in persisted
    assert _CURRENT_URL not in persisted
    assert base64.b64encode(quoted).decode("ascii") not in persisted


async def test_real_dsh_all_four_supported_rasters_are_officially_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real JPEG/PNG/GIF/WebP inputs pass the official rc.2 preparation seam."""
    sources = {
        "image/png": _source_image("PNG", size=(24, 24), color=(255, 0, 0)),
        "image/jpeg": _source_image("JPEG", size=(24, 24), color=(0, 255, 0)),
        "image/gif": _source_image("GIF", size=(24, 24), color=(0, 0, 255)),
        "image/webp": _source_image("WEBP", size=(24, 24), color=(255, 255, 0)),
    }
    urls: dict[str, str] = {}
    responses: dict[str, str] = {}
    for index, (media_type, data) in enumerate(sources.items()):
        url = f"https://example.invalid/image/{index}"
        urls[media_type] = url
        responses[url] = _data_uri(data, media_type.rsplit("/", 1)[1].upper())
    downloader = _StaticDownloader(responses)
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    # Use WebP because the fixture encoder supports every listed source.
    responses[urls["image/webp"]] = _data_uri(sources["image/webp"], "WEBP")

    async with running_dsh(
        tmp_path,
        responses=(
            ToolCallResponse("final_response", {"content": "四格式回复"}),
            ToolCallResponse(
                "record_favorability_delta",
                {"delta": 0, "reason": "格式测试"},
            ),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
        input_modalities=("text", "image"),
    ) as (host, server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        result = await service.generate(
            message=_message(0, "四种图片"),
            recent_messages=[],
            reply_context=None,
            recall_session=fake_recall_session(
                FakeHindsightMemory(response=recall_response())
            ),
            request_trace_id="chat-native-real-2",
            config=_config(),
            image_urls=list(urls.values()),
        )

    assert result.content == "四格式回复"
    image_parts = _image_urls(server.requests[0])
    assert len(image_parts) == 4
    media_types = [part.split(";", 1)[0].removeprefix("data:") for part in image_parts]
    assert all(media.startswith("image/") for media in media_types)
    assert all(media in {"image/jpeg", "image/png", "image/gif", "image/webp"} for media in media_types)


async def test_text_only_route_receives_official_placeholder_without_a_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured text-only model gets the official placeholder, not probing."""
    source = _source_image("PNG", size=(64, 32), color=(20, 40, 60))
    downloader = _StaticDownloader({_CURRENT_URL: _data_uri(source, "PNG")})
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )

    async with running_dsh(
        tmp_path,
        responses=(
            ToolCallResponse("final_response", {"content": "文字模型回复"}),
            ToolCallResponse(
                "record_favorability_delta",
                {"delta": 0, "reason": "占位符测试"},
            ),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
        input_modalities=("text",),
    ) as (host, server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        result = await service.generate(
            message=_message(0, "文字模型看图片"),
            recent_messages=[],
            reply_context=None,
            recall_session=fake_recall_session(
                FakeHindsightMemory(response=recall_response())
            ),
            request_trace_id="chat-native-real-text-only",
            config=_config(),
            image_urls=[_CURRENT_URL],
        )

    assert result.content == "文字模型回复"
    first = server.requests[0]
    names = {
        tool["function"]["name"]
        for tool in first.body.get("tools", [])
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    assert names == {"final_response", "record_favorability_delta"}
    assert "read_image" not in names
    rendered = repr(first.body)
    assert "image omitted because this model accepts text only" in rendered
    assert "data:image/" not in rendered
    assert _image_urls(first) == []
    # No capability request happened before the model call.
    assert len(server.requests) == 2


async def test_terminal_image_model_failure_maps_to_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal image-bearing provider failure is safe and never a text success."""
    from tests.komari_agent.dsh_test_support import HttpErrorResponse

    source = _source_image("PNG", size=(32, 32), color=(1, 2, 3))
    downloader = _StaticDownloader({_CURRENT_URL: _data_uri(source, "PNG")})
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )

    async with running_dsh(
        tmp_path,
        responses=(
            HttpErrorResponse(
                400,
                f"provider refused {_CURRENT_URL} data:image/png;base64,QUJD",
                "invalid_request_error",
                "invalid_request",
            ),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
        input_modalities=("text", "image"),
    ) as (host, _server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        with pytest.raises(failure_module.ImageUnderstandingFailureError) as excinfo:
            await service.generate(
                message=_message(0, "图片失败"),
                recent_messages=[],
                reply_context=None,
                recall_session=fake_recall_session(
                    FakeHindsightMemory(response=recall_response())
                ),
                request_trace_id="chat-native-real-provider-fail",
                config=_config(),
                image_urls=[_CURRENT_URL],
            )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.failed_images == 1
    assert summary.error_types == ("vision_failed",)
    assert summary.stages == ("vision",)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    rendered = str(excinfo.value)
    assert _CURRENT_URL not in rendered
    assert "QUJD" not in rendered
    assert "provider refused" not in rendered


async def test_transient_image_provider_retry_success_does_not_emit_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The official retry can recover without latching an image failure."""
    from tests.komari_agent.dsh_test_support import HttpErrorResponse

    source = _source_image("PNG", size=(32, 32), color=(9, 8, 7))
    downloader = _StaticDownloader({_CURRENT_URL: _data_uri(source, "PNG")})
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )

    async with running_dsh(
        tmp_path,
        responses=(
            HttpErrorResponse(429, "rate limited", "rate_limit_error", "rate_limit"),
            ToolCallResponse("final_response", {"content": "重试后回复"}),
            ToolCallResponse(
                "record_favorability_delta",
                {"delta": 0, "reason": "重试成功"},
            ),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
        input_modalities=("text", "image"),
    ) as (host, _server):
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        result = await service.generate(
            message=_message(0, "重试图片"),
            recent_messages=[],
            reply_context=None,
            recall_session=fake_recall_session(
                FakeHindsightMemory(response=recall_response())
            ),
            request_trace_id="chat-native-real-retry",
            config=_config(),
            image_urls=[_CURRENT_URL],
        )

    assert result.content == "重试后回复"
    assert result.image_failure_summary is None


async def test_cancellation_during_image_model_request_releases_owned_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a live image task releases its owner cache before close."""
    from tests.komari_agent.dsh_test_support import HangResponse

    source = _source_image("PNG", size=(32, 32), color=(4, 5, 6))
    downloader = _StaticDownloader({_CURRENT_URL: _data_uri(source, "PNG")})
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    hang = HangResponse()

    async with running_dsh(
        tmp_path,
        responses=(hang,),
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
        input_modalities=("text", "image"),
    ) as (host, server):
        root = host.attachment_root
        assert root is not None
        service = service_module.DshChatService(
            host_getter=lambda: host,
            knowledge_search=_empty_search,
            uid_search=_empty_uid,
            favorability_getter=_favorability,
            group_bindings_getter=_group_bindings,
        )
        task = asyncio.create_task(
            service.generate(
                message=_message(0, "取消图片请求"),
                recent_messages=[],
                reply_context=None,
                recall_session=fake_recall_session(
                    FakeHindsightMemory(response=recall_response())
                ),
                request_trace_id="chat-native-real-cancel",
                config=_config(),
                image_urls=[_CURRENT_URL],
            )
        )
        await asyncio.to_thread(server.wait_for_request_count, 1)
        assert any(path.is_dir() for path in root.iterdir())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(5):
            while any(path.is_dir() for path in root.iterdir()):  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        hang.release.set()

    assert not root.exists()
