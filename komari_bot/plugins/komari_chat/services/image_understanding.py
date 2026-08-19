"""图片理解模式与下载预算的冻结值对象（TSK-194 / ADR-0010）。

图片理解显式分为 ``native`` 与 ``delegated`` 两种模式：

- ``native``：图片经安全下载与校验后，作为多模态输入直接嵌入主回复
  Agent 的聊天槽位（chat 模型承担原生视觉理解）；不暴露 ``read_image``
  工具，模型拒图时明确失败，不自动降级；
- ``delegated``：只向回复 Agent 暴露稳定图片索引，由 Agent 按需调用
  独立视觉模型（vision 槽位）取得文字描述；``read_image`` 是唯一的
  读图入口，主工具循环始终使用聊天模型与 chat 槽位，不做整循环切换。

模式与下载预算在任务起点从 ``komari_chat`` 配置读取一次并冻结为不可变
值对象；普通 / debug / 简单三条入口共用同一份冻结快照。任务执行期间
配置变更不影响进行中的任务，只作用于下一个任务。

本模块不依赖 NoneBot 运行时，只定义冻结值对象的深模块边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from .image_downloader import ImageDownloadPolicy

#: 合法图片理解模式取值（无兼容值 / 宽松规范化；与配置 Schema 一致）。
VALID_IMAGE_UNDERSTANDING_MODES: frozenset[str] = frozenset(
    {"native", "delegated"}
)


@dataclass(frozen=True, slots=True)
class ImageUnderstandingPolicy:
    """任务起点冻结的图片理解模式与下载预算值对象。"""

    mode: Literal["native", "delegated"]
    download: ImageDownloadPolicy

    @property
    def is_delegated(self) -> bool:
        """是否为委托读图模式（暴露 read_image 工具）。"""
        return self.mode == "delegated"

    @classmethod
    def from_config(cls, config: object) -> "ImageUnderstandingPolicy":
        """任务起点从配置读取一次并冻结；整个任务不再重读配置。

        模式字段来自 ``komari_chat`` 动态配置：缺失或非法值一律明确
        失败，不做旧快照 / 测试替身兼容（协调式破坏升级；0015 之后
        生产 typed 配置必然携带 ``image_understanding_mode``）。下载
        预算委托 ``ImageDownloadPolicy`` 逐项读取并保留跨字段约束。
        """
        mode = getattr(config, "image_understanding_mode", None)
        if mode is None:
            msg = (
                "配置缺少图片理解模式字段（image_understanding_mode），"
                "无法冻结图片理解策略"
            )
            raise RuntimeError(msg)
        if mode not in VALID_IMAGE_UNDERSTANDING_MODES:
            msg = (
                "配置的图片理解模式非法（image_understanding_mode="
                f"{mode!r}），必须为 native 或 delegated"
            )
            raise RuntimeError(msg)
        return cls(
            mode=cast("Literal['native', 'delegated']", mode),
            download=ImageDownloadPolicy.from_config(config),
        )


__all__ = [
    "VALID_IMAGE_UNDERSTANDING_MODES",
    "ImageUnderstandingPolicy",
]
