"""图片下载器测试。"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any, cast

from komari_bot.plugins.komari_chat.services import image_downloader

if TYPE_CHECKING:
    from types import TracebackType

    import pytest


def _encode_data_uri(data: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _download_with_fake_session(session: _FakeSession, url: str) -> str | None:
    return asyncio.run(
        image_downloader._download_single_image(cast("Any", session), url)
    )


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.read_calls = 0
        self.iter_chunked_calls = 0

    async def read(self, _size: int = -1) -> bytes:
        self.read_calls += 1
        return self._chunks[0] if self._chunks else b""

    async def iter_chunked(self, _size: int):
        self.iter_chunked_calls += 1
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        content_length: int | None,
        content_type: str,
        body: bytes,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.content_length = content_length
        self.content_type = content_type
        self._body = body
        self.content = _FakeContent(chunks or [body])
        self.read_calls = 0

    async def read(self) -> bytes:
        self.read_calls += 1
        return self._body


class _FakeRequestContext:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self) -> _FakeResponse:
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise RuntimeError
        return self._response

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        del exc_type, exc, tb
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def get(self, _url: str) -> _FakeRequestContext:
        item = self._responses[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            return _FakeRequestContext(error=item)
        return _FakeRequestContext(response=item)


def test_download_single_image_reads_known_length_response_to_eof() -> None:
    body = b"abcdef"
    response = _FakeResponse(
        content_length=len(body),
        content_type="image/png",
        body=body,
        chunks=[b"abc", b"def"],
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(session, "https://example.com/a.png")

    assert result == _encode_data_uri(body, "image/png")
    assert response.read_calls == 1
    assert response.content.read_calls == 0


def test_download_single_image_reads_chunked_response_until_eof() -> None:
    response = _FakeResponse(
        content_length=None,
        content_type="image/jpeg",
        body=b"wrong-body",
        chunks=[b"ab", b"cd", b"ef"],
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(session, "https://example.com/a.jpg")

    assert result == _encode_data_uri(b"abcdef", "image/jpeg")
    assert response.read_calls == 0
    assert response.content.iter_chunked_calls == 1


def test_download_single_image_rejects_oversized_chunked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_downloader, "_MAX_IMAGE_SIZE", 5)
    response = _FakeResponse(
        content_length=None,
        content_type="image/png",
        body=b"",
        chunks=[b"abc", b"def"],
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(session, "https://example.com/a.png")

    assert result is None


def test_download_single_image_retries_temporary_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(image_downloader.asyncio, "sleep", _no_sleep)
    first = _FakeResponse(
        status=404,
        content_length=0,
        content_type="image/png",
        body=b"",
    )
    second = _FakeResponse(
        status=200,
        content_length=4,
        content_type="image/png",
        body=b"done",
    )
    session = _FakeSession([first, second])

    result = _download_with_fake_session(session, "https://example.com/a.png")

    assert result == _encode_data_uri(b"done", "image/png")
    assert session.calls == 2
