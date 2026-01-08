"""
SR 插件的命令模式实现。

提供可撤销的 add/del 操作，使用命令模式封装操作逻辑。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


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
        config = self.config_manager.get()
        sr_list = config.sr_list

        if self.item in sr_list:
            return f"❌ '{self.item}' 已在神人榜中"

        sr_list.append(self.item)
        self.config_manager.update_field("sr_list", sr_list)

        return f"✅ 已添加 '{self.item}' 到神人榜"

    async def undo(self) -> str:
        """撤销添加操作（从列表中移除）。

        Returns:
            撤销结果消息
        """
        config = self.config_manager.get()
        sr_list = config.sr_list

        if self.item not in sr_list:
            return f"⚠️ 无法撤销：'{self.item}' 不在列表中（可能已被其他操作修改）"

        sr_list.remove(self.item)
        self.config_manager.update_field("sr_list", sr_list)

        return f"↩️ 已撤销添加 '{self.item}'"

    @classmethod
    def from_dict(cls, data: dict, config_manager: Any) -> "AddCommand":
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
        config = self.config_manager.get()
        sr_list = config.sr_list

        # 序号删除模式
        if self.index is not None:
            if 1 <= self.index <= len(sr_list):
                self.item = sr_list[self.index - 1]  # 保存用于 undo
                sr_list.pop(self.index - 1)
                self.config_manager.update_field("sr_list", sr_list)
                return f"✅ 已删除第 {self.index} 位: '{self.item}'"
            return f"❌ 序号 {self.index} 超出范围（1-{len(sr_list)}）"

        # 名称删除模式（原有逻辑）
        if self.item is None:
            return "❌ 删除失败：未指定名称或序号"

        if self.item not in sr_list:
            return f"❌ '{self.item}' 不在神人榜中"

        sr_list.remove(self.item)
        self.config_manager.update_field("sr_list", sr_list)

        return f"✅ 已删除 '{self.item}'"

    async def undo(self) -> str:
        """撤销删除操作（重新添加到列表）。

        Returns:
            撤销结果消息
        """
        if self.item is None:
            return "⚠️ 无法撤销：删除时未记录名称"

        config = self.config_manager.get()
        sr_list = config.sr_list

        if self.item in sr_list:
            return f"⚠️ 无法撤销：'{self.item}' 已在列表中（可能已被其他操作添加）"

        # 如果是序号删除，尝试恢复到原位置；否则追加到末尾
        if self.index is not None and 1 <= self.index <= len(sr_list) + 1:
            sr_list.insert(self.index - 1, self.item)
        else:
            sr_list.append(self.item)

        self.config_manager.update_field("sr_list", sr_list)

        return f"↩️ 已撤销删除 '{self.item}'"

    @classmethod
    def from_dict(cls, data: dict, config_manager: Any) -> "DeleteCommand":
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
