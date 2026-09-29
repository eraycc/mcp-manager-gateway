# CLI 参考

安装后以下命令等价：

~~~console
mmg
mcp-manager
mcp-manager-gateway
~~~

使用 `--help` 查看当前版本的准确参数。

## 全局选项

~~~console
mmg --version
mmg --help
mmg --home <MMG_HOME> <command>
~~~

`--home` 选择独立配置与数据目录。

## `serve`

启动 Web 控制台和 MCP 网关：

~~~console
mmg serve
mmg serve --port 8766
~~~

直接执行 `mmg` 等价于启动服务。

## `stdio`

将本地 stdio MCP 客户端桥接到运行中的 HTTP 网关：

~~~console
mmg stdio --url http://127.0.0.1:8765 --token <MCP_MANAGER_TOKEN>
~~~

也支持环境变量：

- `MCP_MANAGER_URL`
- `MCP_MANAGER_TOKEN`

## `upgrade`

~~~console
mmg upgrade
~~~

只应用数据库 Schema 迁移，不更新安装包。应在网关停止时执行。软件包升级见 [部署与升级](../operations/deployment-and-upgrades.md)。

## `rebuild-logs`

~~~console
mmg rebuild-logs
~~~

从 JSONL 调用日志重建查询索引。它不会把查询索引当作日志原文来源。

## `migrate-db`

~~~console
mmg migrate-db --target-database-url "<DATABASE_URL>"
~~~

把配置数据复制到空的 SQLite 或 MySQL 目标。执行前停止网关。

## `migrate-home`

~~~console
mmg --home <NEW_HOME> migrate-home --from-data <OLD_DATA_DIR>
~~~

可选增加 `--from-env <OLD_ENV_FILE>`。目标主目录必须为空，源实例必须停止。
