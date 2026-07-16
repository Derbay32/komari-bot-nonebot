from importlib import import_module

import nonebot

# bot.py 是容器专用入口，这里用运行时导入避免应用工厂重复执行启动分支。
import_module("bot")

app = nonebot.get_asgi()
