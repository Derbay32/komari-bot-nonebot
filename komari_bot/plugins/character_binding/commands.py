"""角色绑定 OneBot 命令入口（TSK-277 起退役）。

TSK-277 起，角色绑定交互只经 QQ 群 @ 官 Bot 的 ``/bind`` 向导
（``qq_commands.bind_qq``）进行。原 ``bind`` / ``bind_set`` / ``bind_del`` /
``bind_list`` 四个 ``on_command`` matcher 已物理退役；本模块不再注册任何
matcher，也不再提供 OneBot 命令解析接缝，仅保留模块位置以维持既有静态
边界扫描（``tests/group_admission/test_system_static.py``）。
"""

from __future__ import annotations

__all__: list[str] = []
