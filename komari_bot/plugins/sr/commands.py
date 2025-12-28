"""
SR 插件的命令模式实现。

提供可撤销的 add/del 操作，使用命令模式封装操作逻辑。
"""

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .config_schema import DynamicConfigSchema


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
        pass

    @abstractmethod
    async def undo(self) -> str:
        """撤销命令。

        Returns:
            用户可见的撤销结果消息
        """
        pass


@dataclass
class AddCommand(Command):
    """添加神人到列表的命令。

    Attributes:
        item: 要添加的神人名称
        config_manager: 配置管理器，用于持久化
        dynamic_config: 动态配置引用
    """

    item: str
    config_manager: Any
    dynamic_config: "DynamicConfigSchema"

    async def execute(self) -> str:
        """执行添加操作。

        Returns:
            执行结果消息
        """
        sr_list = self.dynamic_config.sr_list

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
        sr_list = self.dynamic_config.sr_list

        if self.item not in sr_list:
            return f"⚠️ 无法撤销：'{self.item}' 不在列表中（可能已被其他操作修改）"

        sr_list.remove(self.item)
        self.config_manager.update_field("sr_list", sr_list)

        return f"↩️ 已撤销添加 '{self.item}'"


@dataclass
class DeleteCommand(Command):
    """从列表中删除神人的命令。

    Attributes:
        item: 要删除的神人名称
        config_manager: 配置管理器，用于持久化
        dynamic_config: 动态配置引用
    """

    item: str
    config_manager: Any
    dynamic_config: "DynamicConfigSchema"

    async def execute(self) -> str:
        """执行删除操作。

        Returns:
            执行结果消息
        """
        sr_list = self.dynamic_config.sr_list

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
        sr_list = self.dynamic_config.sr_list

        if self.item in sr_list:
            return f"⚠️ 无法撤销：'{self.item}' 已在列表中（可能已被其他操作添加）"

        sr_list.append(self.item)
        self.config_manager.update_field("sr_list", sr_list)

        return f"↩️ 已撤销删除 '{self.item}'"


# 模块级别的撤销栈
_undo_stack: deque[Command] = deque(maxlen=5)


def get_undo_stack() -> deque[Command]:
    """获取撤销栈。

    Returns:
        撤销栈的引用
    """
    return _undo_stack


def clear_undo_stack() -> None:
    """清空撤销栈。"""
    _undo_stack.clear()
