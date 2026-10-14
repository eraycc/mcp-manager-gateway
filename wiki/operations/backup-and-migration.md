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

SQLite 或 MySQL 中的配置和目录引用保持权威；此次清理不修改表结构、内容哈希、调用日志保留规则或对象 GC。

- JSONL 删除会原子重写，只保留各 key 的最新存活值，不保留历史正文和 null tombstone。重写失败保留原文件，后续维护可重试。
- 启动后的首次后台维护及后续每小时检查会压缩目录缓存，并分批回收无当前目录引用或属于旧模型的数据库向量。
- 旧向量 JSONL 中无法识别作用域的哈希记录会被清空并按需重建。新缓存记录仅增加模型哈希、服务/revision 引用和缓存时间；这些元数据不参与内容哈希，也不会发送给 embedding 服务。
- 可回收的活动向量缓存保留最多 2048 项、最长 7 天，避免查询和 Token 作用域累积。禁用服务及发现失败作用域的可识别缓存保持不动；启用或修复后再按正常规则回收。
- 缓存重写运行于后台线程，不在删除服务或切换模型时同步清理向量。
- 旧 `jobs/jobs.jsonl` 及旧任务 JSON 不再读取或导入。主目录迁移仍原样复制 `jobs/`；应用不会自动删除旧文件。确认数据库任务正常、备份旧文件后，可自行清理这些历史文件。

## 日志索引

调用日志原文是 JSONL，查询索引可以重建：

~~~console
mmg rebuild-logs
~~~

离线重建前停止网关。运行中的实例可以使用控制台提供的后台重建任务。
