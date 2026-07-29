# character_binding 角色名绑定存储迁移至 PostgreSQL

为统一项目数据存储架构（业务数据归 PostgreSQL），character_binding 的存储从 `data/character_binding/bindings.json`（三层文件锁 + fsync 原子替换 + stat 签名刷新）迁移到 `komari_character_bindings` 表，连接池与生命周期对齐 user_data 范式。`get_character_name()` 保持同步接口与内存快照，快照仅在进程内写路径（`.bind` / `.debug bind` / 未来管理 API）自更新；**刻意不引入** revision 轮询（对比 user_ban）或 LISTEN/NOTIFY，因为部署层 `docker/gunicorn_conf.py` 硬性强制单 worker，所有常规写路径都在同一进程内。直接改数据库属运维后门，重启后生效。

## Consequences

- 文件锁、fsync、原子替换、stat 签名刷新与公开的文件刷新导出全部移除；新增一次性迁移脚本 `scripts/migrate_character_binding_to_pg.py`。
- PG 启动不可用时以空快照降级（角色名回退昵称/QQ 号），写入报错，延续原文件损坏时的故障语义。
- **失效条件**：未来若放开多 worker 部署，本决策的前提即告失效，必须重新引入跨进程刷新机制（revision 轮询或 LISTEN/NOTIFY），否则其他 worker 的快照将永久过期。
