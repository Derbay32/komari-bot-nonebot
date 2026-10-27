"""固定字体初始化准备测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Self, cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from komari_bot.core.project_paths import DATA_DIR
from komari_bot.plugins.group_history_summary import font_asset

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from aiohttp.web_request import Request
    from aiohttp.web_response import StreamResponse

    FontHandler = Callable[[Request], Awaitable[StreamResponse]]

FONT_BYTES = b"fixed-font-test-payload"


@pytest.fixture
def isolated_font_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """让需要下载/落盘的用例只操作测试目录，不触碰仓库内字体。"""
    target = tmp_path / "app" / "data" / "fonts" / "LXGWWenKaiGBScreen.ttf"
    monkeypatch.setattr(font_asset, "FONT_PATH", target)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    return target


def _own_temp_files(target: Path) -> list[Path]:
    return list(target.parent.glob(f".{target.name}.*.tmp"))


@asynccontextmanager
async def _font_server(
    monkeypatch: pytest.MonkeyPatch,
    handler: FontHandler,
) -> AsyncIterator[list[Request]]:
    """启动仅监听本机的受控 HTTP 字体端点。"""
    requests: list[Request] = []

    async def _wrapped(request: Request) -> StreamResponse:
        requests.append(request)
        return await handler(request)

    app = web.Application()
    app.router.add_get("/LXGWWenKaiGBScreen.ttf", _wrapped)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(
        font_asset,
        "FONT_URL",
        str(server.make_url("/LXGWWenKaiGBScreen.ttf")),
    )
    try:
        yield requests
    finally:
        await server.close()


@asynccontextmanager
async def _streaming_font_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: bytes,
    started: asyncio.Event,
    release: asyncio.Event,
) -> AsyncIterator[list[Request]]:
    """先发送部分正文并暂停，供测试观察正式文件尚未出现。"""

    async def _handler(request: Request) -> StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        await response.prepare(request)
        split_at = max(1, len(payload) // 2)
        await response.write(payload[:split_at])
        started.set()
        await release.wait()
        await response.write(payload[split_at:])
        await response.write_eof()
        return response

    async with _font_server(monkeypatch, _handler) as requests:
        yield requests


class _ScriptedContent:
    def __init__(
        self,
        *,
        chunks: tuple[bytes, ...] = (),
        error: BaseException | None = None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.chunks = chunks
        self.error = error
        self.started = started
        self.release = release

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error


class _ScriptedResponse:
    def __init__(
        self,
        content: _ScriptedContent,
        *,
        status: int = 200,
    ) -> None:
        self.content = content
        self.status = status
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=cast("Any", None),
                history=(),
                status=self.status,
                message="scripted HTTP failure",
            )


class _ScriptedSession:
    def __init__(self, response: _ScriptedResponse) -> None:
        self.response = response
        self.closed = False
        self.requests = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    def get(self, _url: str) -> _ScriptedResponse:
        self.requests += 1
        return self.response


def _install_scripted_sessions(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_ScriptedResponse],
) -> list[_ScriptedSession]:
    sessions: list[_ScriptedSession] = []
    pending = iter(responses)

    def _factory(*_args: object, **_kwargs: object) -> _ScriptedSession:
        session = _ScriptedSession(next(pending))
        sessions.append(session)
        return session

    monkeypatch.setattr(font_asset.aiohttp, "ClientSession", _factory)
    return sessions


def test_fixed_font_contract_and_bounded_timeout() -> None:
    assert font_asset.FONT_PATH.is_absolute()
    assert font_asset.FONT_PATH == DATA_DIR / "fonts" / "LXGWWenKaiGBScreen.ttf"
    assert font_asset.FONT_URL == (
        "https://github.com/lxgw/LxgwWenKai-Screen/releases/download/"
        "v1.521/LXGWWenKaiGBScreen.ttf"
    )
    assert font_asset._DOWNLOAD_TIMEOUT.total == 120
    assert font_asset._DOWNLOAD_TIMEOUT.connect == 10
    assert font_asset._DOWNLOAD_TIMEOUT.sock_connect == 10
    assert font_asset._DOWNLOAD_TIMEOUT.sock_read == 30


async def test_existing_target_returns_without_network_or_validation(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing corrupt bytes are intentionally accepted")

    def _unexpected_session(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError

    monkeypatch.setattr(font_asset.aiohttp, "ClientSession", _unexpected_session)
    await font_asset.ensure_font_asset()

    assert target.read_bytes() == b"existing corrupt bytes are intentionally accepted"
    assert _own_temp_files(target) == []


async def test_missing_target_ignores_other_font_and_creates_directory(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    target.parent.mkdir(parents=True)
    other_font = target.parent / "AnotherFont.ttf"
    other_font.write_bytes(b"not the fixed font")
    assert not target.exists()

    async def _handler(_request: Request) -> web.Response:
        return web.Response(body=FONT_BYTES)

    async with _font_server(monkeypatch, _handler) as requests:
        await font_asset.ensure_font_asset()

    assert len(requests) == 1
    assert target.read_bytes() == FONT_BYTES
    assert other_font.read_bytes() == b"not the fixed font"
    assert _own_temp_files(target) == []


async def test_final_file_appears_only_after_complete_stream(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    started = asyncio.Event()
    release = asyncio.Event()
    assert not target.parent.exists()

    async with _streaming_font_server(
        monkeypatch,
        payload=FONT_BYTES,
        started=started,
        release=release,
    ) as requests:
        task = asyncio.create_task(font_asset.ensure_font_asset())
        await asyncio.wait_for(started.wait(), timeout=5)
        assert target.parent.is_dir()
        assert not target.exists()
        temp_files = _own_temp_files(target)
        assert len(temp_files) == 1
        assert temp_files[0].parent == target.parent
        assert temp_files[0].name.startswith(f".{target.name}.")

        release.set()
        await task

    assert len(requests) == 1
    assert target.read_bytes() == FONT_BYTES
    assert _own_temp_files(target) == []


async def test_http_error_removes_own_temp_and_creates_no_final(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    response = _ScriptedResponse(_ScriptedContent(), status=503)
    sessions = _install_scripted_sessions(monkeypatch, [response])

    with pytest.raises(aiohttp.ClientResponseError):
        await font_asset.ensure_font_asset()

    assert len(sessions) == 1
    assert sessions[0].requests == 1
    assert sessions[0].closed is True
    assert not target.exists()
    assert _own_temp_files(target) == []


async def test_timeout_removes_own_temp_and_creates_no_final(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    response = _ScriptedResponse(
        _ScriptedContent(chunks=(b"partial",), error=TimeoutError())
    )
    sessions = _install_scripted_sessions(monkeypatch, [response])

    with pytest.raises(TimeoutError):
        await font_asset.ensure_font_asset()

    assert len(sessions) == 1
    assert sessions[0].closed is True
    assert not target.exists()
    assert _own_temp_files(target) == []


async def test_stream_interruption_removes_own_temp_and_creates_no_final(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    response = _ScriptedResponse(
        _ScriptedContent(
            chunks=(b"partial",),
            error=aiohttp.ClientPayloadError("connection lost"),
        )
    )
    sessions = _install_scripted_sessions(monkeypatch, [response])

    with pytest.raises(aiohttp.ClientPayloadError):
        await font_asset.ensure_font_asset()

    assert len(sessions) == 1
    assert sessions[0].closed is True
    assert not target.exists()
    assert _own_temp_files(target) == []


async def test_cancellation_removes_own_temp_and_creates_no_final(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    started = asyncio.Event()
    release = asyncio.Event()
    response = _ScriptedResponse(
        _ScriptedContent(
            chunks=(b"partial",),
            started=started,
            release=release,
        )
    )
    sessions = _install_scripted_sessions(monkeypatch, [response])

    task = asyncio.create_task(font_asset.ensure_font_asset())
    await asyncio.wait_for(started.wait(), timeout=5)
    assert not target.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sessions) == 1
    assert sessions[0].closed is True
    assert not target.exists()
    assert _own_temp_files(target) == []


async def test_new_invocation_retries_after_failure_without_internal_retry(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    failure = _ScriptedResponse(
        _ScriptedContent(
            chunks=(b"first attempt",),
            error=aiohttp.ClientPayloadError("interrupted"),
        )
    )
    success = _ScriptedResponse(_ScriptedContent(chunks=(FONT_BYTES,)))
    sessions = _install_scripted_sessions(monkeypatch, [failure, success])

    with pytest.raises(aiohttp.ClientPayloadError):
        await font_asset.ensure_font_asset()

    assert len(sessions) == 1
    assert sessions[0].requests == 1
    assert not target.exists()

    await font_asset.ensure_font_asset()

    assert len(sessions) == 2
    assert all(session.requests == 1 for session in sessions)
    assert all(session.closed for session in sessions)
    assert target.read_bytes() == FONT_BYTES
    assert _own_temp_files(target) == []


async def test_restart_after_success_uses_existing_target_without_second_request(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path

    async def _handler(_request: Request) -> web.Response:
        return web.Response(body=FONT_BYTES)

    async with _font_server(monkeypatch, _handler) as requests:
        await font_asset.ensure_font_asset()
        assert len(requests) == 1

        def _unexpected_session(*_args: object, **_kwargs: object) -> Any:
            raise AssertionError

        monkeypatch.setattr(font_asset.aiohttp, "ClientSession", _unexpected_session)
        await font_asset.ensure_font_asset()

    assert len(requests) == 1
    assert target.read_bytes() == FONT_BYTES
    assert _own_temp_files(target) == []


async def test_unrelated_files_and_temp_files_are_preserved(
    isolated_font_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = isolated_font_path
    target.parent.mkdir(parents=True)
    unrelated_font = target.parent / "Unrelated.ttf"
    unrelated_temp = target.parent / ".another-download.tmp"
    unrelated_font.write_bytes(b"keep font")
    unrelated_temp.write_bytes(b"keep temp")

    async def _handler(_request: Request) -> web.Response:
        return web.Response(body=FONT_BYTES)

    async with _font_server(monkeypatch, _handler):
        await font_asset.ensure_font_asset()

    assert unrelated_font.read_bytes() == b"keep font"
    assert unrelated_temp.read_bytes() == b"keep temp"
    assert target.read_bytes() == FONT_BYTES
    assert _own_temp_files(target) == []
