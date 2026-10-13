# SQLite 与 MySQL 部署、迁移和备份

MMG 支持 SQLite 与 MySQL 8.0/8.4。主数据库保存身份、权限、目录索引、任务状态和对象引用；`DATA_DIR` 还保存 `secret.key`、不可变详情对象、本地日志与查询索引。无论使用哪种主数据库，都必须持久化并一起备份数据库和完整 `DATA_DIR`。

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

该命令幂等回填可检索审批名称，并导入旧目录/任务索引。旧的运行中任务只标记为 `interrupted`，不会自动重放。

## 备份与恢复

备份集合必须包含：

- SQLite 文件，或 MySQL 的一致性备份；
- 完整 `DATA_DIR`，包括 `secret.key`、`artifacts/`、`logs/` 和 `indexes/`；
- 实际生效的非敏感配置说明；凭据用密钥管理系统另行保存。

恢复时先恢复数据库和同一时点的 `DATA_DIR`，确认 `secret.key` 未变化，再运行 `mmg upgrade`。不要只恢复数据库：对象引用可能仍存在，但详情文件会缺失。

目录采用渐进式、冷启动友好的策略：列表和搜索读取持久化摘要/索引，不启动 lazy MCP；完整定义和下游连接仅在显式详情、刷新或调用时按需获取。大型目录应依赖分页和缓存，不把全部工具 Schema 放入一个响应。

## API 兼容

`/api/v1` 和 `gateway_*` 的分页、精确 `gateway_name`、完整 `inputSchema` 合同保持稳定。旧的整包兼容接口在当前 1.x 主版本内保留明确的字节上限；任何移除都应在下一主版本前发布迁移说明，调用方不应依赖无限制全量 JSON。
