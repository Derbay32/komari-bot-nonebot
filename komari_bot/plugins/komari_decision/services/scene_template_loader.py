"""Scene 模板加载与标准化。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_SCENE_TEMPLATE_PATH = Path("config") / "prompts" / "komari_memory_scenes.yaml"


@dataclass(frozen=True)
class SceneTemplateItem:
    """标准化后的 scene 条目。"""

    scene_key: str
    scene_type: str  # fixed | general
    content_text: str
    enabled: bool
    order_index: int
    content_hash: str


@dataclass(frozen=True)
class SceneTemplatePayload:
    """标准化模板载荷。"""

    source_path: str
    source_hash: str
    fixed_candidates: dict[str, str]
    general_scenes: list[dict[str, str]]
    items: list[SceneTemplateItem]


class SceneTemplateLoader:
    """读取 YAML 并输出标准化 scene 条目。"""

    def __init__(self, template_path: str | Path | None = None) -> None:
        self._template_path = (
            Path(template_path) if template_path is not None else _DEFAULT_SCENE_TEMPLATE_PATH
        )

    def resolve_template_path(self) -> Path:
        """解析模板绝对路径。"""
        if self._template_path.is_absolute():
            return self._template_path
        return self._template_path.resolve()

    @staticmethod
    def compute_text_hash(text: str) -> str:
        """计算文本哈希。"""
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def compute_source_hash(payload: dict) -> str:
        """计算模板源哈希。"""
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_fixed_candidates(raw_fixed: object) -> dict[str, str]:
        if not isinstance(raw_fixed, dict):
            msg = "fixed_candidates 必须是对象"
            raise TypeError(msg)

        normalized: dict[str, str] = {}
        for key, value in raw_fixed.items():
            key_str = str(key).strip()
            value_str = str(value).strip()
            if not key_str or not value_str:
                continue
            normalized[key_str] = value_str

        if not normalized:
            msg = "fixed_candidates 不能为空"
            raise ValueError(msg)

        return normalized

    @staticmethod
    def _normalize_general_scenes(raw_scenes: object) -> list[dict[str, str]]:
        if not isinstance(raw_scenes, list):
            msg = "general_scenes 必须是数组"
            raise TypeError(msg)

        normalized: list[dict[str, str]] = []
        for item in raw_scenes:
            if not isinstance(item, dict):
                continue
            scene_id = str(item.get("id", "")).strip()
            scene_text = str(item.get("text", "")).strip()
            if not scene_id or not scene_text:
                continue
            normalized.append({"id": scene_id, "text": scene_text})

        if not normalized:
            msg = "general_scenes 不能为空"
            raise ValueError(msg)

        return normalized

    @staticmethod
    def normalize_scene_items(
        fixed_candidates: dict[str, str],
        general_scenes: list[dict[str, str]],
    ) -> list[SceneTemplateItem]:
        """将模板归一为可入库条目列表。"""
        items: list[SceneTemplateItem] = []
        order = 0

        for key, content in fixed_candidates.items():
            items.append(
                SceneTemplateItem(
                    scene_key=key,
                    scene_type="fixed",
                    content_text=content,
                    enabled=True,
                    order_index=order,
                    content_hash=SceneTemplateLoader.compute_text_hash(content),
                )
            )
            order += 1

        for scene in general_scenes:
            content = scene["text"]
            items.append(
                SceneTemplateItem(
                    scene_key=scene["id"],
                    scene_type="general",
                    content_text=content,
                    enabled=True,
                    order_index=order,
                    content_hash=SceneTemplateLoader.compute_text_hash(content),
                )
            )
            order += 1

        return items

    def load_scene_template(self) -> SceneTemplatePayload:
        """加载并标准化模板。"""
        path = self.resolve_template_path()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError as e:
            msg = f"读取 scene 模板失败: {path}"
            raise RuntimeError(msg) from e
        except yaml.YAMLError as e:
            msg = f"scene 模板 YAML 解析失败: {path}"
            raise RuntimeError(msg) from e

        if not isinstance(raw, dict):
            msg = "scene 模板根节点必须是对象"
            raise TypeError(msg)

        fixed_candidates = self._normalize_fixed_candidates(raw.get("fixed_candidates", {}))
        general_scenes = self._normalize_general_scenes(raw.get("general_scenes", []))
        items = self.normalize_scene_items(fixed_candidates, general_scenes)

        normalized_payload = {
            "fixed_candidates": fixed_candidates,
            "general_scenes": general_scenes,
            "items": [
                {
                    "scene_key": i.scene_key,
                    "scene_type": i.scene_type,
                    "content_hash": i.content_hash,
                    "enabled": i.enabled,
                    "order_index": i.order_index,
                }
                for i in items
            ],
        }

        return SceneTemplatePayload(
            source_path=str(path),
            source_hash=self.compute_source_hash(normalized_payload),
            fixed_candidates=fixed_candidates,
            general_scenes=general_scenes,
            items=items,
        )
