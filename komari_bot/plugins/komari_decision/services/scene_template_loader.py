"""Scene 模板加载与标准化。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml

_DEFAULT_SCENE_TEMPLATE_PATH = Path("config") / "prompts" / "komari_memory_scenes.yaml"
_PG_SOURCE_PATH = "postgresql:komari_decision_scenes"
_REQUIRED_FIXED_KEYS = ("NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION")

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..repositories.scene_repository import SceneRepository


@dataclass(frozen=True)
class SceneTemplateItem:
    """标准化后的 scene 条目。"""

    scene_key: str
    scene_type: str  # fixed | general
    content_text: str
    enabled: bool
    order_index: int
    content_hash: str
    scene_id: int | None = None


@dataclass(frozen=True)
class SceneTemplatePayload:
    """标准化模板载荷。"""

    source_path: str
    source_hash: str
    fixed_candidates: dict[str, str]
    general_scenes: list[dict[str, str]]
    items: list[SceneTemplateItem]


class SceneTemplateLoaderProtocol(Protocol):
    """Scene 模板加载器协议。"""

    def load_scene_template(self) -> SceneTemplatePayload | Awaitable[SceneTemplatePayload]: ...


class YamlSceneTemplateLoader:
    """读取 YAML 并输出标准化 scene 条目，仅供迁移脚本和测试使用。"""

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
                    content_hash=YamlSceneTemplateLoader.compute_text_hash(content),
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
                    content_hash=YamlSceneTemplateLoader.compute_text_hash(content),
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


class PostgresSceneTemplateLoader:
    """从 PostgreSQL scene 内容表加载运行时模板。"""

    def __init__(self, repository: SceneRepository) -> None:
        self.repository = repository

    @staticmethod
    def _validate_scenes(rows: list[dict]) -> None:
        keys: set[str] = set()
        general_count = 0
        for row in rows:
            scene_key = str(row.get("scene_key") or "").strip()
            scene_type = str(row.get("scene_type") or "").strip()
            content_text = str(row.get("content_text") or "").strip()
            if not scene_key:
                msg = "scene_key 不能为空"
                raise ValueError(msg)
            if scene_key in keys:
                msg = f"scene_key 重复: {scene_key}"
                raise ValueError(msg)
            keys.add(scene_key)
            if scene_type not in {"fixed", "general"}:
                msg = f"scene_type 非法: {scene_key}={scene_type}"
                raise ValueError(msg)
            if not content_text:
                msg = f"scene 内容不能为空: {scene_key}"
                raise ValueError(msg)
            if scene_type == "general":
                general_count += 1

        missing = [key for key in _REQUIRED_FIXED_KEYS if key not in keys]
        if missing:
            msg = f"PostgreSQL scenes 缺少必需 fixed keys: {missing}"
            raise ValueError(msg)
        if general_count <= 0:
            msg = "PostgreSQL scenes 至少需要 1 个 enabled general scene"
            raise ValueError(msg)

    async def load_scene_template(self) -> SceneTemplatePayload:
        """加载 PG scene 内容表并构造模板载荷。"""
        rows = await self.repository.list_scenes(enabled_only=True)
        self._validate_scenes(rows)

        fixed_candidates: dict[str, str] = {}
        general_scenes: list[dict[str, str]] = []
        items: list[SceneTemplateItem] = []
        for row in rows:
            scene_key = str(row["scene_key"])
            scene_type = str(row["scene_type"])
            content_text = str(row["content_text"])
            if scene_type == "fixed":
                fixed_candidates[scene_key] = content_text
            else:
                general_scenes.append({"id": scene_key, "text": content_text})
            items.append(
                SceneTemplateItem(
                    scene_key=scene_key,
                    scene_type=scene_type,
                    content_text=content_text,
                    enabled=bool(row.get("enabled", True)),
                    order_index=int(row.get("order_index") or 0),
                    content_hash=str(row["content_hash"]),
                    scene_id=int(row["id"]),
                )
            )

        return SceneTemplatePayload(
            source_path=_PG_SOURCE_PATH,
            source_hash=self.repository.compute_scene_source_hash(rows),
            fixed_candidates=fixed_candidates,
            general_scenes=general_scenes,
            items=items,
        )


SceneTemplateLoader = YamlSceneTemplateLoader
