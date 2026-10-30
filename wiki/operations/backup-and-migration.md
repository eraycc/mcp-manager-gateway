# 备份与迁移

## 必须备份的内容

停止写入后备份：

- MMG 主目录中的 `.env`；
- `DATA_DIR` 全部内容；
- 配置数据库；
- `secret.key`。

数据库和 `secret.key` 必须成对保留，否则已有加密 Token、OAuth 凭据和服务秘密无法解密。

能力缓存和日志索引可以重建，但业务审计与调用原文应根据合规要求保存。

## 恢复演练

有效备份不仅是复制成功，还应在隔离环境验证：

1. 数据库能够打开；
2. Schema 可以迁移；
3. 管理员能够登录；
4. 加密凭据可以解密；
5. MCP 配置和授权范围存在；
6. 代表性目录与工具能够刷新。

不要在生产实例运行时把恢复数据覆盖回原目录。

## 迁移旧主目录

停止源实例，准备空目标目录：

~~~console
mmg --home <NEW_HOME> migrate-home --from-data <OLD_DATA_DIR>
~~~

旧版本另有配置文件时：

~~~console
mmg --home <NEW_HOME> migrate-home --from-data <OLD_DATA_DIR> --from-env <OLD_ENV_FILE>
~~~

命令会拒绝非空目标或不安全的运行中迁移。迁移后先在新端口验证，再切换服务。

## 数据库迁移

停止网关，准备空目标数据库：

~~~console
mmg migrate-db --target-database-url "mysql://mcp_user:<PASSWORD>@db.example.com:3306/mcp_manager?charset=utf8mb4"
~~~

支持 SQLite 与 MySQL 之间迁移。完成后更新 `DATABASE_URL` 再启动。日志与能力缓存仍由 `DATA_DIR` 管理，不会自动跟随数据库 URL 移动。

## 缓存与历史残留

主库保存权威配置、授权、目录控制和任务/审批证据引用；目录、词项、向量与查询缓存位于单一派生库，详细正文位于分域 CAS。

- 缓存域与持久域允许保存同一内容的两份对象，以保持清理边界独立；域内按 SHA-256 去重。
- 后台回收分批移除旧目录倒排索引、过期查询缓存和无引用对象。当前版本及最多两个、最长 24 小时的历史 manifest 保留正文引用。
- 相同服务、owner、模型、Schema 与描述性内容的目录向量可以复用；显式撤权立即失效，刷新失败按原失败阈值保留上一版能力。
- 旧目录 JSON/JSONL 仅在首次布局迁移或显式 reconcile 时校验导入；已完成迁移的普通启动不重复全量复制或删除主库表。无法识别的旧缓存保留供人工处理。
- 旧 `jobs/jobs.jsonl` 及旧任务 JSON 不再读取或导入，目录迁移仍保留原文件。
- `backups/` 中的升级备份和旧数据归档不会随缓存 GC 擅自删除。恢复验证和备份保留应单独管理。

## 一次性存储布局升级

停止旧版本实例并备份数据库、完整 DATA_DIR 和密钥。新版本首次启动默认自动执行升级；`STORAGE_MIGRATION_MODE=manual` 可要求先离线迁移：

~~~console
mmg storage status
mmg storage migrate --dry-run
mmg storage migrate
~~~

迁移先获得目录独占权，保存并校验备份，分批复制、核对作用域和摘要，最后通过受控 schema 迁移删除主库中六类可重建表。保留配置和持久引用，不进行长期双写。失败后保留最近可恢复阶段，再执行同一命令续行；完成后的启动跳过数据复制。旧 `indexes/logs.sqlite` 在安全关闭后迁移为 `indexes/derived.sqlite`，调用日志写入 `logs/calls/`，旧日志源仍可查询。

这些命令必须与同目录运行实例互斥；不要使用 Alembic stamp 跳过切换校验。详见[存储治理](storage-governance.md)。

## 日志索引

调用日志原文是 JSONL，查询索引可以重建：

~~~console
mmg rebuild-logs
~~~

离线重建前停止网关。运行中的实例可以使用控制台提供的后台重建任务。
