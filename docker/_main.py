from importlib import import_module

import nonebot

# bot.py 由 Docker 构建阶段动态生成，这里用运行时导入避免本地静态检查误报。
import_module("bot")

app = nonebot.get_asgi()
