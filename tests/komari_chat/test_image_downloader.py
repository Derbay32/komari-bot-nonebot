"""图片下载器测试。"""

from __future__ import annotations

import asyncio
import base64
import socket
from io import BytesIO
from typing import TYPE_CHECKING, Any, cast

import pytest
from PIL import Image

from komari_bot.plugins.komari_chat.services import image_downloader

if TYPE_CHECKING:
    from types import TracebackType


def _make_image_bytes(
    image_format: str = "PNG",
    size: tuple[int, int] = (2, 2),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(12, 34, 56)).save(buffer, format=image_format)
    return buffer.getvalue()


def _encode_data_uri(data: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _download_with_fake_session(
    session: _FakeSession,
    url: str,
    policy: image_downloader.ImageDownloadPolicy | None = None,
    budget: image_downloader._DownloadBudget | None = None,
) -> str | None:
    return asyncio.run(
        image_downloader._download_single_image(
            cast("Any", session),
            url,
            policy,
            budget,
        )
    )


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.iter_chunked_calls = 0

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
        headers: dict[str, str] | None = None,
        url: str = "https://93.184.216.34/a.png",
    ) -> None:
        self.status = status
        self.content_length = content_length
        self.content_type = content_type
        self.headers = headers or {}
        self.url = url
        self.content = _FakeContent(chunks or [body])


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

    def get(self, _url: str, **_kwargs: object) -> _FakeRequestContext:
        item = self._responses[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            return _FakeRequestContext(error=item)
        return _FakeRequestContext(response=item)


def test_download_single_image_streams_known_length_response_to_eof() -> None:
    body = _make_image_bytes()
    midpoint = len(body) // 2
    response = _FakeResponse(
        content_length=len(body),
        content_type="image/png",
        body=body,
        chunks=[body[:midpoint], body[midpoint:]],
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(session, "https://93.184.216.34/a.png")

    assert result == _encode_data_uri(body, "image/png")
    assert response.content.iter_chunked_calls == 1


def test_download_single_image_streams_chunked_response_until_eof() -> None:
    body = _make_image_bytes("JPEG")
    response = _FakeResponse(
        content_length=None,
        content_type="image/jpeg",
        body=body,
        chunks=[body[:8], body[8:21], body[21:]],
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(session, "https://93.184.216.34/a.jpg")

    assert result == _encode_data_uri(body, "image/jpeg")
    assert response.content.iter_chunked_calls == 1


def test_download_single_image_uses_detected_format_instead_of_headers() -> None:
    body = _make_image_bytes("PNG")
    response = _FakeResponse(
        content_length=len(body),
        content_type="image/jpeg",
        body=body,
        url="https://93.184.216.34/spoofed.jpg",
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(
        session,
        "https://93.184.216.34/spoofed.jpg",
    )

    assert result == _encode_data_uri(body, "image/png")


def test_download_single_image_rejects_non_image_body() -> None:
    body = b"<html>not an image</html>"
    response = _FakeResponse(
        content_length=len(body),
        content_type="image/png",
        body=body,
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(session, "https://93.184.216.34/a.png")

    assert result is None


def test_download_single_image_rejects_truncated_image_after_magic_match() -> None:
    body = _make_image_bytes()[:-12]
    response = _FakeResponse(
        content_length=len(body),
        content_type="image/png",
        body=body,
    )
    session = _FakeSession([response])

    result = _download_with_fake_session(session, "https://93.184.216.34/a.png")

    assert result is None


def test_download_single_image_rejects_excessive_pixel_count() -> None:
    body = _make_image_bytes(size=(100, 100))
    response = _FakeResponse(
        content_length=len(body),
        content_type="image/png",
        body=body,
    )
    session = _FakeSession([response])
    policy = image_downloader.ImageDownloadPolicy(max_pixels=9_999)

    result = _download_with_fake_session(
        session,
        "https://93.184.216.34/a.png",
        policy,
    )

    assert result is None


def test_download_single_image_rejects_oversized_chunked_response() -> None:
    response = _FakeResponse(
        content_length=None,
        content_type="image/png",
        body=b"",
        chunks=[b"abc", b"def"],
    )
    session = _FakeSession([response])
    policy = image_downloader.ImageDownloadPolicy(
        max_image_bytes=5,
        max_total_bytes=10,
    )

    result = _download_with_fake_session(
        session,
        "https://93.184.216.34/a.png",
        policy,
    )

    assert result is None


def test_downloads_share_atomic_total_byte_budget() -> None:
    body = _make_image_bytes()
    policy = image_downloader.ImageDownloadPolicy(
        max_image_bytes=len(body),
        max_total_bytes=len(body) * 2 - 1,
    )
    budget = image_downloader._DownloadBudget(policy.max_total_bytes)
    first = _FakeResponse(
        content_length=len(body),
        content_type="image/png",
        body=body,
    )
    second = _FakeResponse(
        content_length=len(body),
        content_type="image/png",
        body=body,
    )
    session = _FakeSession([first, second])

    async def _download_both() -> tuple[str | None, str | None]:
        first_result = await image_downloader._download_single_image(
            cast("Any", session),
            "https://93.184.216.34/first.png",
            policy,
            budget,
        )
        second_result = await image_downloader._download_single_image(
            cast("Any", session),
            "https://93.184.216.34/second.png",
            policy,
            budget,
        )
        return first_result, second_result

    first_result, second_result = asyncio.run(_download_both())

    assert first_result is not None
    assert second_result is None
    assert budget.consumed_bytes == len(body)


def test_total_byte_budget_consume_is_atomic_under_concurrency() -> None:
    budget = image_downloader._DownloadBudget(max_total_bytes=5)

    async def _consume_concurrently() -> list[bool]:
        return list(await asyncio.gather(budget.consume(3), budget.consume(3)))

    results = asyncio.run(_consume_concurrently())

    assert sorted(results) == [False, True]
    assert budget.consumed_bytes == 3


def test_download_single_image_retries_temporary_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(image_downloader.asyncio, "sleep", _no_sleep)
    body = _make_image_bytes()
    first = _FakeResponse(
        status=404,
        content_length=0,
        content_type="image/png",
        body=b"",
    )
    second = _FakeResponse(
        status=200,
        content_length=len(body),
        content_type="image/png",
        body=body,
    )
    session = _FakeSession([first, second])

    result = _download_with_fake_session(session, "https://93.184.216.34/a.png")

    assert result == _encode_data_uri(body, "image/png")
    assert session.calls == 2


def test_download_batch_limits_image_count_and_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_downloads = 0
    max_active_downloads = 0
    downloaded_urls: list[str] = []

    async def _fake_download(
        _session: object,
        url: str,
        _policy: image_downloader.ImageDownloadPolicy,
        _budget: image_downloader._DownloadBudget,
    ) -> str:
        nonlocal active_downloads, max_active_downloads
        active_downloads += 1
        max_active_downloads = max(max_active_downloads, active_downloads)
        downloaded_urls.append(url)
        await asyncio.sleep(0)
        active_downloads -= 1
        return f"data:{url}"

    monkeypatch.setattr(image_downloader, "_download_single_image", _fake_download)
    policy = image_downloader.ImageDownloadPolicy(max_images=2, concurrency=1)

    results = asyncio.run(
        image_downloader.download_images_as_base64_aligned(
            ["https://example.com/1", "https://example.com/2", "https://example.com/3"],
            policy,
        )
    )

    assert results == [
        "data:https://example.com/1",
        "data:https://example.com/2",
        None,
    ]
    assert downloaded_urls == ["https://example.com/1", "https://example.com/2"]
    assert max_active_downloads == 1


def test_download_batch_enforces_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _slow_download(
        _session: object,
        _url: str,
        _policy: image_downloader.ImageDownloadPolicy,
        _budget: image_downloader._DownloadBudget,
    ) -> str:
        await asyncio.sleep(1)
        return "data:late"

    monkeypatch.setattr(image_downloader, "_download_single_image", _slow_download)
    policy = image_downloader.ImageDownloadPolicy(total_timeout_seconds=0.01)

    results = asyncio.run(
        image_downloader.download_images_as_base64_aligned(
            ["https://example.com/1", "https://example.com/2"],
            policy,
        )
    )

    assert results == [None, None]


def test_normalize_image_source_rejects_data_uri() -> None:
    assert image_downloader._normalize_image_source("data:image/png;base64,AAAA") is None
    assert (
        image_downloader._normalize_image_source("https://93.184.216.34/a.png")
        == "https://93.184.216.34/a.png"
    )


def test_validate_download_url_rejects_blocked_targets() -> None:
    blocked_urls = [
        "http://127.0.0.1/a.jpg",
        "http://localhost/a.jpg",
        "http://10.0.0.1/a.jpg",
        "http://172.16.0.1/a.jpg",
        "http://192.168.1.1/a.jpg",
        "http://169.254.169.254/latest/meta-data/",
        "https://user@example.com/a.jpg",
        "https://example.com:8443/a.jpg",
    ]

    async def _validate_all() -> list[bool]:
        return [
            await image_downloader._validate_download_url(url) for url in blocked_urls
        ]

    assert asyncio.run(_validate_all()) == [False] * len(blocked_urls)


def test_validate_download_url_allows_public_ip_and_domain() -> None:
    async def _validate_all() -> tuple[bool, bool]:
        return (
            await image_downloader._validate_download_url(
                "https://93.184.216.34/a.png"
            ),
            await image_downloader._validate_download_url(
                "https://example.com/a.png"
            ),
        )

    assert asyncio.run(_validate_all()) == (True, True)


def test_connection_resolver_rejects_mixed_public_and_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(image_downloader.socket, "getaddrinfo", _fake_getaddrinfo)
    resolver = image_downloader._PublicAddressResolver()

    async def _resolve() -> None:
        with pytest.raises(OSError, match="内网或保留地址"):
            await resolver.resolve("attacker.example", 443, socket.AF_INET)

    asyncio.run(_resolve())


def test_connection_resolver_returns_only_validated_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]

    monkeypatch.setattr(image_downloader.socket, "getaddrinfo", _fake_getaddrinfo)
    resolver = image_downloader._PublicAddressResolver()

    results = asyncio.run(resolver.resolve("example.com", 443, socket.AF_INET))

    assert results[0]["hostname"] == "example.com"
    assert results[0]["host"] == "93.184.216.34"


def test_batch_connector_rejects_dns_rebinding_target_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = 0

    def _fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        nonlocal resolve_calls
        resolve_calls += 1
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]

    monkeypatch.setattr(image_downloader.socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(image_downloader, "_DOWNLOAD_RETRY_ATTEMPTS", 1)

    result = asyncio.run(
        image_downloader.download_images_as_base64_aligned(
            ["https://rebind.example/image.png"]
        )
    )

    assert result == [None]
    assert resolve_calls == 1


def test_download_single_image_rejects_redirect_to_blocked_network() -> None:
    redirect = _FakeResponse(
        status=302,
        content_length=0,
        content_type="text/plain",
        body=b"",
        headers={"Location": "http://127.0.0.1/a.png"},
        url="https://93.184.216.34/a.png",
    )
    session = _FakeSession([redirect])

    result = _download_with_fake_session(session, "https://93.184.216.34/a.png")

    assert result is None
    assert session.calls == 1


def test_download_single_image_rejects_too_many_redirects() -> None:
    responses: list[_FakeResponse | Exception] = [
        _FakeResponse(
            status=302,
            content_length=0,
            content_type="text/plain",
            body=b"",
            headers={"Location": f"https://93.184.216.34/{index}.png"},
            url=f"https://93.184.216.34/{index - 1}.png",
        )
        for index in range(1, 5)
    ]
    session = _FakeSession(responses)

    result = _download_with_fake_session(session, "https://93.184.216.34/0.png")

    assert result is None
    assert session.calls == 4
