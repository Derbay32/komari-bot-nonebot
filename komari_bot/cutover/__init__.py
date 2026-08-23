"""TSK-232 cutover 命令行工具包（operator 停机切换专用）。

本包是统一群聊准入 cutover 的操作面入口：六个子命令（``audit`` /
``prepare-policy`` / ``capture-evidence`` / ``finalize-redis`` /
``abort-pre-backfill`` / ``status``）经 ``komari_bot.cutover.cli.main``
驱动，全部输出为单行 ``sort_keys`` JSON（成功 ``status=ok`` 退出 0；
失败 ``status=error`` + ``error_code`` 退出非零），且只包含 closed code
与聚合 count，绝不投影动态身份。

设计纪律：本包不导入任何 ``komari_bot.plugins.*`` 模块（无插件 import
副作用）；PostgreSQL 经 asyncpg 直连、Redis 经 redis-py 直连，不依赖
NoneBot 运行时。写命令与 0012/0013 迁移共用同一稳定 advisory lock 键，
锁被占用时以 ``LOCK_BUSY`` 快速失败。
"""
