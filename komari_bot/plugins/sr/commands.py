"""
SR 插件的命令模式实现。

提供可撤销的 add/del 操作，使用命令模式封装操作逻辑。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


class Command(ABC):
    """命令基类，定义执行和撤销的接口。

    所有具体命令必须实现 execute 和 undo 方法。
    """

    @abstractmethod
    async def execute(self) -> str:
        """执行命令。

        Returns:
            用户可见的执行结果消息
        """

    @abstractmethod
    async def undo(self) -> str:
        """撤销命令。

        Returns:
            用户可见的撤销结果消息
        """


@dataclass
class AddCommand(Command):
    """添加神人到列表的命令。

    Attributes:
        item: 要添加的神人名称
        config_manager: 配置管理器，用于持久化和获取配置
    """

    item: str
    config_manager: Any

    async def execute(self) -> str:
        """执行添加操作。

        Returns:
            执行结果消息
        """
        added = False

        def _add_item(current_value: Any) -> list[str]:
            nonlocal added
            sr_list = [str(item) for item in current_value]
            added = self.item not in sr_list
            if added:
                sr_list.append(self.item)
            return sr_list

        await self.config_manager.mutate_field_async("sr_list", _add_item)
        if not added:
            return f"❌ '{self.item}' 已在神人榜中"
        return f"✅ 已添加 '{self.item}' 到神人榜"

    async def undo(self) -> str:
        """撤销添加操作（从列表中移除）。

        Returns:
            撤销结果消息
        """
        removed = False

        def _remove_item(current_value: Any) -> list[str]:
            nonlocal removed
            sr_list = [str(item) for item in current_value]
            removed = self.item in sr_list
            if removed:
                sr_list.remove(self.item)
            return sr_list

        await self.config_manager.mutate_field_async("sr_list", _remove_item)
        if not removed:
            return f"⚠️ 无法撤销：'{self.item}' 不在列表中（可能已被其他操作修改）"
        return f"↩️ 已撤销添加 '{self.item}'"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], config_manager: Any) -> "AddCommand":
        """从字典恢复命令对象。

        Args:
            data: 包含命令数据的字典
            config_manager: 配置管理器实例

        Returns:
            AddCommand 实例
        """
        return cls(item=data["item"], config_manager=config_manager)


@dataclass
class DeleteCommand(Command):
    """从列表中删除神人的命令。

    支持按名称或按序号删除。

    Attributes:
        item: 要删除的神人名称（名称删除模式）
        index: 要删除的序号（序号删除模式，1-indexed）
        config_manager: 配置管理器，用于持久化和获取配置
    """

    item: str | None = None
    index: int | None = None
    config_manager: Any = None

    async def execute(self) -> str:
        """执行删除操作。

        支持两种删除模式：
        - 按序号删除：当 index 不为 None 时使用
        - 按名称删除：当 item 不为 None 时使用

        Returns:
            执行结果消息
        """
        if self.index is None and self.item is None:
            return "❌ 删除失败：未指定名称或序号"

        requested_item = self.item
        removed_item: str | None = None
        observed_length = 0

        def _delete_item(current_value: Any) -> list[str]:
            nonlocal observed_length, removed_item
            sr_list = [str(item) for item in current_value]
            observed_length = len(sr_list)
            removed_item = None

            if self.index is not None:
                if 1 <= self.index <= len(sr_list):
                    removed_item = sr_list.pop(self.index - 1)
                return sr_list

            if requested_item in sr_list:
                removed_item = requested_item
                sr_list.remove(requested_item)
            return sr_list

        await self.config_manager.mutate_field_async("sr_list", _delete_item)
        if removed_item is None:
            if self.index is not None:
                return f"❌ 序号 {self.index} 超出范围（1-{observed_length}）"
            return f"❌ '{requested_item}' 不在神人榜中"

        self.item = removed_item
        if self.index is not None:
            return f"✅ 已删除第 {self.index} 位: '{removed_item}'"
        return f"✅ 已删除 '{removed_item}'"

    async def undo(self) -> str:
        """撤销删除操作（重新添加到列表）。

        Returns:
            撤销结果消息
        """
        if self.item is None:
            return "⚠️ 无法撤销：删除时未记录名称"

        item = self.item
        restored = False

        def _restore_item(current_value: Any) -> list[str]:
            nonlocal restored
            sr_list = [str(item) for item in current_value]
            restored = item not in sr_list
            if not restored:
                return sr_list

            if self.index is not None and 1 <= self.index <= len(sr_list) + 1:
                sr_list.insert(self.index - 1, item)
            else:
                sr_list.append(item)
            return sr_list

        await self.config_manager.mutate_field_async("sr_list", _restore_item)
        if not restored:
            return f"⚠️ 无法撤销：'{self.item}' 已在列表中（可能已被其他操作添加）"
        return f"↩️ 已撤销删除 '{self.item}'"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], config_manager: Any) -> "DeleteCommand":
        """从字典恢复命令对象。

        Args:
            data: 包含命令数据的字典
            config_manager: 配置管理器实例

        Returns:
            DeleteCommand 实例
        """
        return cls(
            item=data.get("item"),
            index=data.get("index"),
            config_manager=config_manager,
        )
