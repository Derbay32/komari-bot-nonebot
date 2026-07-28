"""容器内 NoneBot 初始化入口。"""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from komari_bot.common.nonebot_compat import install_nonebot_forwardref_compatibility

install_nonebot_forwardref_compatibility()
nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_builtin_plugins("echo")
nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
