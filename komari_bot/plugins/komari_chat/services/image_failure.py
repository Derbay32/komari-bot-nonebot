"""Neutral, bounded image-failure values shared by image transport and chat.

This module deliberately carries no URL, base64 payload, cache path, provider
text, or visual description. Both the trusted native image transport and the
notification boundary can depend on it without importing a delegated-vision
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Fixed task-level codes for the three image failure categories. The first is
#: an invalid descriptor/chunk/digest contract, the second is a verified source
#: that official preparation/projection could not accept, and the third is a
#: terminal model request failure on an image-bearing task.
INVALID_IMAGE_INPUT_CODE = "invalid_image_input"
IMAGE_INPUT_FAILED_CODE = "image_input_failed"
IMAGE_MODEL_FAILED_CODE = "image_model_failed"

ImageFailureMode = Literal["native", "delegated"]


@dataclass(frozen=True, slots=True)
class ImageFailureSummary:
    """Safe aggregate of image failures (no URLs, bytes, or content)."""

    mode: ImageFailureMode = "native"
    all_images_unavailable: bool = False
    total_images: int = 0
    attempted_images: int = 0
    failed_images: int = 0
    error_types: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()


class ImageUnderstandingFailureError(RuntimeError):
    """Terminal image input failure carrying only a safe aggregate summary."""

    def __init__(self, summary: ImageFailureSummary) -> None:
        self.summary = summary
        super().__init__(f"图片理解失败（mode={summary.mode}）")


__all__ = [
    "IMAGE_INPUT_FAILED_CODE",
    "IMAGE_MODEL_FAILED_CODE",
    "INVALID_IMAGE_INPUT_CODE",
    "ImageFailureMode",
    "ImageFailureSummary",
    "ImageUnderstandingFailureError",
]
