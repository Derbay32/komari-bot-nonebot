"""Trusted native image transport for one DSH chat task.

Python keeps the existing safe download boundary and task-owned bytes. Node
fetches those bytes through the internal ``chat.read_image_chunk`` callback;
only a closed descriptor (index, origin, media type, byte count, digest) is
placed in task data. No URL, data URI, base64 payload, or cache path crosses
the bridge.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from nonebot import logger

from .image_downloader import ImageDownloadPolicy, ImageDownloadSession
from .image_failure import ImageFailureSummary

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

ImageOrigin = Literal["quoted", "current"]
ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]

MAX_TRUSTED_IMAGE_COUNT = 8
MAX_TRUSTED_IMAGE_BYTES = 16 * 1024 * 1024
MAX_TRUSTED_AGGREGATE_BYTES = 32 * 1024 * 1024
MAX_TRUSTED_CHUNK_BYTES = 128 * 1024
_ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)


class NativeImageContractError(RuntimeError):
    """A descriptor, chunk, or source size violated the trusted transport."""


class ImageChunkAdmissionError(RuntimeError):
    """The task's current admission denied a chunk or download effect."""

    callback_code = "admission_denied"


class ImageChunkRejectedError(RuntimeError):
    """The requested chunk is outside the live task-owned source."""

    callback_code = "invalid_image_input"


@dataclass(frozen=True, slots=True)
class ChatImageDescriptor:
    """Closed descriptor placed in ``ChatTaskData.images``."""

    index: int
    origin: ImageOrigin
    media_type: ImageMediaType
    byte_count: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "origin": self.origin,
            "media_type": self.media_type,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
        }


class _OwnedImage:
    """Decoded source bytes owned by one task; repr never includes payloads."""

    __slots__ = ("byte_count", "data", "index", "media_type", "origin", "sha256")
    index: int
    origin: ImageOrigin
    media_type: ImageMediaType
    data: bytes | None
    byte_count: int
    sha256: str

    def __init__(
        self,
        *,
        index: int,
        origin: ImageOrigin,
        media_type: ImageMediaType,
        data: bytes,
    ) -> None:
        self.index = index
        self.origin = origin
        self.media_type = media_type
        self.data: bytes | None = data
        self.byte_count = len(data)
        self.sha256 = hashlib.sha256(data).hexdigest()

    def __repr__(self) -> str:
        return (
            f"_OwnedImage(index={self.index}, origin={self.origin}, "
            f"media_type={self.media_type!r}, byte_count={self.byte_count})"
        )

    def descriptor(self) -> ChatImageDescriptor:
        return ChatImageDescriptor(
            index=self.index,
            origin=self.origin,
            media_type=self.media_type,
            byte_count=self.byte_count,
            sha256=self.sha256,
        )

    def close(self) -> None:
        self.data = None


class NativeImageInput:
    """One task's safely downloaded native images and trusted chunk reader."""

    __slots__ = (
        "_admission_guard",
        "_closed",
        "_download_failures",
        "_downloader",
        "_effective_total",
        "_group_id",
        "_images",
        "_policy",
        "_request_trace_id",
        "_sources",
    )

    def __init__(
        self,
        *,
        quoted_sources: list[str],
        current_sources: list[str],
        policy: ImageDownloadPolicy,
        admission_guard: Callable[[], bool],
        group_id: str | None,
        request_trace_id: str,
    ) -> None:
        self._policy = policy
        self._admission_guard = admission_guard
        self._group_id = group_id
        self._request_trace_id = request_trace_id
        self._downloader = ImageDownloadSession(policy)
        self._images: dict[int, _OwnedImage] = {}
        self._download_failures = 0
        self._effective_total = 0
        self._closed = False

        selected: list[tuple[int, ImageOrigin, str]] = []
        effective_limit = min(policy.max_images, MAX_TRUSTED_IMAGE_COUNT)
        origins: tuple[tuple[ImageOrigin, list[str]], ...] = (
            ("quoted", quoted_sources),
            ("current", current_sources),
        )
        for origin, sources in origins:
            for source in sources:
                if len(selected) >= effective_limit:
                    break
                selected.append((len(selected), origin, source))
            if len(selected) >= effective_limit:
                break
        self._sources = selected
        self._effective_total = len(selected)

    def __repr__(self) -> str:
        return (
            "NativeImageInput("
            f"total={self._effective_total}, prepared={len(self._images)}, "
            f"download_failures={self._download_failures}, "
            f"closed={self._closed})"
        )

    @property
    def total_images(self) -> int:
        return self._effective_total

    @property
    def prepared_count(self) -> int:
        return len(self._images)

    @property
    def download_failures(self) -> int:
        return self._download_failures

    @property
    def all_downloads_failed(self) -> bool:
        return self._effective_total > 0 and not self._images

    def descriptors(self) -> tuple[ChatImageDescriptor, ...]:
        return tuple(
            self._images[index].descriptor() for index in sorted(self._images)
        )

    async def prepare(self) -> tuple[ChatImageDescriptor, ...]:
        """Download and verify this task's sources once, preserving indexes."""
        if not self._sources or self._closed:
            return ()
        tasks = [
            asyncio.create_task(self._download_one(index, origin, source))
            for index, origin, source in self._sources
        ]
        admission_revoked = False
        try:
            results = await asyncio.gather(*tasks)
        except ImageChunkAdmissionError:
            admission_revoked = True
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        else:
            for image in results:
                if image is not None:
                    self._images[image.index] = image
            self._download_failures = self._effective_total - len(self._images)
        if admission_revoked:
            await self.aclose()
            raise ImageChunkAdmissionError
        if not self._admission_guard():
            await self.aclose()
            raise ImageChunkAdmissionError
        aggregate = sum(image.byte_count for image in self._images.values())
        if aggregate > MAX_TRUSTED_AGGREGATE_BYTES:
            await self.aclose()
            raise NativeImageContractError
        logger.info(
            "[KomariChat] native image preparation: trace_id={} total={} prepared={} failed={}",
            self._request_trace_id,
            self._effective_total,
            len(self._images),
            self._download_failures,
        )
        return self.descriptors()

    async def _download_one(
        self,
        index: int,
        origin: ImageOrigin,
        source: str,
    ) -> _OwnedImage | None:
        if not self._admission_guard():
            raise ImageChunkAdmissionError
        try:
            data_uri = await self._downloader.download(source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[KomariChat] native image download error: index={} origin={} error_type={}",
                index,
                origin,
                type(exc).__name__,
            )
            return None
        if not self._admission_guard():
            raise ImageChunkAdmissionError
        if data_uri is None:
            logger.warning(
                "[KomariChat] native image download failed: index={} origin={}",
                index,
                origin,
            )
            return None
        parsed = _decode_data_uri(data_uri)
        if parsed is None:
            logger.warning(
                "[KomariChat] native image payload validation failed: index={} origin={}",
                index,
                origin,
            )
            return None
        media_type, data = parsed
        if len(data) > MAX_TRUSTED_IMAGE_BYTES:
            logger.warning(
                "[KomariChat] native image exceeds source ceiling: index={} bytes={}",
                index,
                len(data),
            )
            return None
        return _OwnedImage(
            index=index,
            origin=origin,
            media_type=media_type,
            data=data,
        )

    async def read_chunk(self, data: Mapping[str, object]) -> dict[str, object]:
        """Return one bounded canonical-base64 source chunk."""
        if self._closed:
            raise ImageChunkRejectedError
        if set(data) != {"image_index", "offset"}:
            raise ImageChunkRejectedError
        image_index = data["image_index"]
        offset = data["offset"]
        if (
            isinstance(image_index, bool)
            or not isinstance(image_index, int)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
        ):
            raise ImageChunkRejectedError
        if not self._admission_guard():
            raise ImageChunkAdmissionError
        image = self._images.get(image_index)
        if image is None or image.data is None:
            raise ImageChunkRejectedError
        if offset < 0 or offset >= image.byte_count:
            raise ImageChunkRejectedError
        end = min(offset + MAX_TRUSTED_CHUNK_BYTES, image.byte_count)
        payload = image.data[offset:end]
        return {
            "image_index": image_index,
            "offset": offset,
            "data": base64.b64encode(payload).decode("ascii"),
            "eof": end == image.byte_count,
        }

    def failure_summary(
        self,
        *,
        processing_failed: bool = False,
        model_failed: bool = False,
    ) -> ImageFailureSummary:
        """Build the safe native summary without inspecting exception text."""
        if processing_failed or model_failed:
            failed = self._effective_total
        else:
            failed = self._download_failures
        error_types: set[str] = set()
        stages: set[str] = set()
        if self._download_failures:
            error_types.add("image_unavailable")
            stages.add("download")
        if processing_failed or model_failed:
            error_types.add("vision_failed")
            stages.add("vision")
        return ImageFailureSummary(
            mode="native",
            all_images_unavailable=self.all_downloads_failed and not (
                processing_failed or model_failed
            ),
            total_images=self._effective_total,
            attempted_images=self._effective_total,
            failed_images=failed,
            error_types=tuple(sorted(error_types)),
            stages=tuple(sorted(stages)),
        )

    async def aclose(self) -> None:
        """Close deterministically and wait for the download client to stop."""
        if not self._closed:
            self._closed = True
            for image in self._images.values():
                image.close()
            self._images.clear()
            self._sources = []
        await self._downloader.close()


def _decode_data_uri(value: str) -> tuple[ImageMediaType, bytes] | None:  # noqa: PLR0911
    """Decode the downloader's trusted data URI without retaining it."""
    if not value.startswith("data:"):
        return None
    try:
        header, payload = value.split(",", 1)
    except ValueError:
        return None
    if not header.endswith(";base64"):
        return None
    media_type = header[5:-7]
    if media_type not in _ALLOWED_MEDIA_TYPES:
        return None
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not decoded or base64.b64encode(decoded).decode("ascii") != payload:
        return None
    return cast("ImageMediaType", media_type), decoded


__all__ = [
    "MAX_TRUSTED_AGGREGATE_BYTES",
    "MAX_TRUSTED_CHUNK_BYTES",
    "MAX_TRUSTED_IMAGE_BYTES",
    "MAX_TRUSTED_IMAGE_COUNT",
    "ChatImageDescriptor",
    "ImageChunkAdmissionError",
    "ImageChunkRejectedError",
    "NativeImageContractError",
    "NativeImageInput",
]
