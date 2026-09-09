"""Help copy for the QQ Russian roulette plugin (TSK-278).

Four confirmed help sections (TSK-266 10.1 / 6a9bf92b) are concatenated into
``PluginMetadata.usage``; ``komari_help`` scans that usage as a single
``command``-category entry.  No ``/轮盘 帮助`` command exists — the parser
treats it as ``unknown_command``.
"""

from __future__ import annotations

#: 开局与等待帮助（含人数上限与开局命令）。
WAITING_HELP = """\
俄罗斯轮盘 · 等候帮助
2～6 人参与，发送 @Bot /轮盘 开局 即可开局。
其他玩家发送“加入”进入本局；局主可“开始”、把局主“转让 <玩家编号>”，
满员或开始后等待区关闭，开局后未加入的成员无法中途上车。"""

#: 行动帮助（开枪/装填/结束/弃权/道具面板）。
ACTION_HELP = """\
俄罗斯轮盘 · 行动帮助
轮到你的回合时，发送“开枪”扣动扳机；实弹出局，空弹继续。
“装填”补充一发空弹；“结束”结束本回合；“弃权”直接出局。
“道具”打开道具面板，道具字母固定为 A＝放大镜、B＝啤酒、C＝连发器、D＝锁；
使用或丢弃时发送“道具 使用 A”这类命令，锁的目标写作 D<玩家编号>（例如 D2）。"""

#: 奖励选择帮助（新道具替换规则）。
REWARD_HELP = """\
俄罗斯轮盘 · 奖励选择帮助
开枪获得新道具且道具格已满时，需要选择处理方式：
发送“奖励 丢弃”丢弃新道具，或“奖励 替换 <道具名>”用新道具换下已有道具。"""

#: 帮助入口页首（插件名称与一句话简介）。
HEADER = """\
俄罗斯轮盘
QQ 群内的俄罗斯轮盘小游戏：轮流开枪、拼运气也拼道具。
@Bot /轮盘 开局 创建新局；更多玩法见下方分节。"""


def help_usage() -> str:
    """PluginMetadata.usage 文案（四份定稿按序拼接）。"""
    return "\n\n".join((HEADER, WAITING_HELP, ACTION_HELP, REWARD_HELP))
