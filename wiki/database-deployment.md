# SQLite 与 MySQL 部署、迁移和备份

MMG 支持 SQLite 与 MySQL 8.0/8.4。主数据库保存身份、权限、配置、目录控制状态、任务状态和持久对象引用；`DATA_DIR` 还保存 `secret.key`、不可变详情对象、本地日志与查询索引。无论使用哪种主数据库，都必须持久化并一起备份数据库和完整 `DATA_DIR`。

## SQLite（默认）

~~~console
docker compose up --build -d
docker compose ps
~~~

默认 Compose 先运行一次 `mcp-manager upgrade`，成功后才启动网关；`/readyz` 只有在数据库初始化完成后才返回成功。数据保存在 `mcp-manager-data` 命名卷。

## MySQL 8.4

先通过安全渠道设置部署专用密码，再启动 MySQL 配置：

~~~console
MYSQL_PASSWORD=<strong-database-password>
MYSQL_ROOT_PASSWORD=<strong-root-password>
docker compose -f compose.mysql.yaml up --build -d
docker compose -f compose.mysql.yaml ps
~~~

`compose.mysql.yaml` 同时持久化 `mysql-data` 和 `mcp-manager-data`，等待 MySQL 健康并完成单次迁移后才启动网关。不要把示例占位符或密码提交到仓库。

非容器部署可设置：

~~~dotenv
DATABASE_URL=mysql://<user>:<password>@<mysql-host>:3306/<database>?charset=utf8mb4
DATA_DIR=/srv/mmg/data
PUBLIC_URL=https://mmg.example.com
COOKIE_SECURE=true
~~~

## 数据库互转

先停止目标 MMG 实例，保留源库、`DATA_DIR` 和 `SECRET_KEY`，并确保目标数据库为空：

~~~console
mmg migrate-db --target-database-url "mysql://<user>:<password>@<mysql-host>:3306/<database>?charset=utf8mb4"
~~~

迁移按最多 250 行分批复制，并在目标事务提交前比较逐表计数与规范化内容哈希。只有报告 `verified=true` 后，才应切换 `DATABASE_URL`。切换后保留源库，完成登录、Token、目录搜索、安全代表性调用和重启验收，再决定归档。

旧版本升级后可执行：

~~~console
mmg reconcile-data
~~~

该命令幂等回填可检索审批名称，并校验、导入可识别作用域的旧目录；旧任务 JSONL 不再导入。数据库中的未完成任务在重启时标记为 `interrupted`，不会自动重放。

## 备份与恢复

备份集合必须包含：

- SQLite 文件，或 MySQL 的一致性备份；
- 完整 `DATA_DIR`，包括 `secret.key`、`objects/`、`catalog/`、`backups/`、`logs/` 和 `indexes/`；
- 实际生效的非敏感配置说明；凭据用密钥管理系统另行保存。

恢复时先恢复数据库和同一时点的 `DATA_DIR`，确认 `secret.key` 未变化，再运行 `mmg upgrade`。不要只恢复数据库：对象引用可能仍存在，但详情文件会缺失。

目录采用渐进式、冷启动友好的策略：列表和搜索读取持久化摘要/索引，不启动 lazy MCP；完整定义和下游连接仅在显式详情、刷新或调用时按需获取。大型目录应依赖分页和缓存，不把全部工具 Schema 放入一个响应。

## 数据库空间治理

管理员进入 **系统设置 → 存储治理**，分别配置主 SQLite 和本地派生索引 `indexes/derived.sqlite`。默认仅在启动时检查一次：空闲页至少 **32 MiB** 且占数据库页空间至少 **20%**，两个条件同时满足才执行 `VACUUM`；阈值可独立调整，也可关闭启动自动回收。不做每小时压缩检查。

“释放数据库空间”通过后台任务主动压缩所选数据库，不受自动阈值限制；界面展示加载状态、实际释放空间和失败原因。“刷新空间信息”只读取少量页统计与文件大小，不扫描业务表。压缩会回收空闲页并整理碎片，不删除配置、有效日志、目录缓存或向量索引，也不会改变日志保留策略。无需为了瘦身清空所有持久化数据。

MySQL 模式不显示主 SQLite 配置，也不对 MySQL 执行 `VACUUM`；派生索引仍是本地 SQLite，可以独立治理。目录和向量的历史残留由有界后台回收和服务/Schema 变更时的增量同步处理；有效且未变化的目录向量不因数据库压缩而过期或重建。容量预算、缓存 TTL 和后台回收见[完整存储治理](operations/storage-governance.md)。

建议在低峰期手动压缩，并预留充足磁盘空间（压缩可能额外需要数据库体积约两倍的空闲空间）。期间 SQLite 写入可能等待；遇到活跃读写锁或空间不足时跳过并提示重试，绝不强删文件。切换页面只停止前端观察，不中断后台压缩。`-wal`、`-shm` 由 SQLite 管理，通过安全 checkpoint 处理，不手动删除。

参见[英文入口](../README.md)、[中文入口](../README.zh-CN.md)和[备份与迁移](operations/backup-and-migration.md)。

## API 兼容

`/api/v1` 和 `gateway_*` 的分页、精确 `gateway_name`、完整 `inputSchema` 合同保持稳定。旧的整包兼容接口在当前 1.x 主版本内保留明确的字节上限；任何移除都应在下一主版本前发布迁移说明，调用方不应依赖无限制全量 JSON。
