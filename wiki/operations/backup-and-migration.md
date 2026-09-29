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

## 日志索引

调用日志原文是 JSONL，查询索引可以重建：

~~~console
mmg rebuild-logs
~~~

离线重建前停止网关。运行中的实例可以使用控制台提供的后台重建任务。
