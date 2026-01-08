"""角色名绑定管理器。"""

import asyncio
import json
from pathlib import Path

from nonebot import logger


class CharacterBindingManager:
    """角色名绑定管理器。"""

    def __init__(self) -> None:
        """初始化绑定管理器。"""
        # 使用独立的 data 目录存储绑定数据
        self.binding_file = Path("data/character_binding/bindings.json")
        self.binding_file.parent.mkdir(parents=True, exist_ok=True)
        self._bindings: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._load_bindings()

    def _load_bindings(self) -> None:
        """从文件加载绑定数据。"""
        if self.binding_file.exists():
            try:
                with Path.open(self.binding_file, encoding="utf-8") as f:
                    self._bindings = json.load(f)
                logger.info(f"[CharacterBinding] 加载角色绑定: {len(self._bindings)} 条")
            except (OSError, json.JSONDecodeError):
                logger.warning("[CharacterBinding] 绑定文件加载失败", exc_info=True)
                self._bindings = {}
        else:
            self._bindings = {}
            # 初始化时同步写入空文件
            try:
                with Path.open(self.binding_file, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=2)
                logger.info("[CharacterBinding] 初始化角色绑定文件")
            except OSError:
                logger.exception("[CharacterBinding] 初始化绑定文件失败")

    async def _save_bindings(self) -> None:
        """保存绑定数据到文件。"""
        async with self._lock:
            try:
                with Path.open(self.binding_file, "w", encoding="utf-8") as f:
                    json.dump(self._bindings, f, ensure_ascii=False, indent=2)
            except OSError:
                logger.exception("[CharacterBinding] 绑定文件保存失败")

    def get_character_name(
        self,
        user_id: str,
        fallback_nickname: str | None = None,
    ) -> str:
        """获取用户的角色名。

        优先级: 绑定名称 > fallback_nickname > user_id

        Args:
            user_id: 用户ID
            fallback_nickname: 备用昵称(如QQ昵称)

        Returns:
            角色名称
        """
        # 1. 检查绑定
        if user_id in self._bindings:
            return self._bindings[user_id]

        # 2. 回退到昵称
        if fallback_nickname:
            return fallback_nickname

        # 3. 最后回退到user_id
        return user_id

    async def set_character_name(self, user_id: str, character_name: str) -> None:
        """设置用户的角色名绑定。

        Args:
            user_id: 用户ID
            character_name: 角色名称
        """
        self._bindings[user_id] = character_name
        await self._save_bindings()
        logger.info(f"[CharacterBinding] 绑定角色: {user_id} -> {character_name}")

    async def remove_character_name(self, user_id: str) -> bool:
        """移除用户的角色名绑定。

        Args:
            user_id: 用户ID

        Returns:
            是否成功移除
        """
        if user_id in self._bindings:
            del self._bindings[user_id]
            await self._save_bindings()
            logger.info(f"[CharacterBinding] 解除绑定: {user_id}")
            return True
        return False

    def list_bindings(self) -> dict[str, str]:
        """获取所有绑定。

        Returns:
            绑定字典 {user_id: character_name}
        """
        return self._bindings.copy()

    def has_binding(self, user_id: str) -> bool:
        """检查用户是否有绑定。

        Args:
            user_id: 用户ID

        Returns:
            是否存在绑定
        """
        return user_id in self._bindings
