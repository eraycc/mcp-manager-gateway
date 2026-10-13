# 能力目录与审批诊断 API

[文档首页](../README.md) · [English README](../../README.md) · [简体中文 README](../../README.zh-CN.md)

这些 Web API 需要控制台身份验证；修改操作还需要 CSRF 验证。网关客户端继续使用[网关工具](gateway-tools.md)，不要以 Web API 绕过审批。

## 摘要与精确详情

- `GET /api/v1/mcps/{id}/capabilities?kind=tools&q=&limit=50` 只返回摘要、匹配总数、代次和 `next_cursor`，不包含完整 Schema，也不启动下游。
- `kind` 可为 `tools`、`resources`、`prompts` 或 `templates`。默认每页 50，服务目录最多 200；实际返回条数还受 128 KiB 页预算限制。
- 跟随 `next_cursor` 时保持 kind、q 和 limit 不变。配置、凭据或目录代次变化后应重新开始分页，不复用旧 cursor。
- `GET /api/v1/mcps/{id}/capabilities/{kind}/{name}` 按精确名称读取一条完整定义。名称作为路径段时应正确进行 URL 编码。
- 空目录不代表连接成功；管理员需要显式执行刷新。只读查询不会替代刷新，也不会将 lazy 服务启动起来。

## 审批测试快照

管理员可调用 `POST /api/v1/mcp-proposals/{id}/test?summary=true`。测试仍真实连接下游，但响应只返回数量、测试版本、run ID 和诊断元数据，最多 16 KiB，不夹带所有工具的 JSON。

测试成功后，使用：

- `GET /api/v1/mcp-proposals/{id}/test-capabilities?kind=tools&q=&limit=50`；
- `GET /api/v1/mcp-proposals/{id}/test-capabilities/{kind}/{name}`。

快照只供运行该测试的管理员访问，绑定提议 revision 和 run ID，24 小时后失效。修改、重新测试、批准或拒绝会使旧快照失效；它不是已批准的 MCP，不可作为网关调用入口。编辑时附上已读取的 `revision`，过期编辑返回 409，需要重新加载。

## 兼容与大小限制

旧 `GET /api/v1/mcps/{id}/tools` 只读取缓存，不自动连接。旧完整目录测试、诊断和刷新响应限制为 1 MiB；服务与提议列表页限制为 128 KiB。超限返回明确的 `catalog_payload_too_large`，不截断 inputSchema，也不返回大块 JSON 让浏览器处理。

大目录集成应使用摘要分页和单条详情；服务/提议列表遇到超限时减小 `page_size`。旧测试响应默认仍兼容小目录；需要大目录诊断时显式使用 `summary=true`。现有目录缓存的显式升级导入另见[备份与迁移](../operations/backup-and-migration.md)。

## MCP protocol pagination

`gateway_search_tools` accepts optional `names_only: true` for complete name traversal without reading definition artifacts. Obtain the original full `inputSchema` through exact search before `gateway_call`. Providers in each response belong only to that page. Follow opaque `next_cursor` with unchanged query and page size; identity, permission, credentials, generation, feature or retrieval changes invalidate it. Native lists use `nextCursor`. `gateway_list_resources` accepts `limit` and `cursor`, returns summary items and a total. Whole JSON-RPC responses include all text and structured representations in the 1 MiB budget. Individual oversized contracts fail explicitly with `catalog_entry_too_large`.

## Console cache and full detail display

检测到旧版缓存时，目录响应带有 `refresh_required: true`，控制台提示管理员刷新。刷新成功后，当前弹窗直接显示新目录。复制服务会自动尝试建立分页目录；连接失败时服务仍保留，并展示可操作的缓存错误。审批中已有成功测试快照时，“能力测试”先提供查看缓存或重新测试的选择；快照过期时需要重新测试。

能力列表保持摘要分页。用户显式打开单项完整定义或执行单工具测试时，控制台显示完整响应，通过后台格式化和分批更新保持界面响应；服务器端传输与存储大小预算仍然有效，超预算返回明确错误而非成功截断。

## Service configuration export

`POST /api/v1/mcps/export` 返回通用 `mcpServers` JSON。stdio 的 `command`、`args`、`env`、`cwd`，以及网络传输的 `url`、`headers` 均位于条目顶层。Streamable HTTP 导出为通用客户端采用的 `type: "http"`；MMG 同时支持导入 `http` 和 `streamable-http`。

```json
{
  "mcpServers": {
    "example": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "example-mcp"],
      "config": {
        "format": "mmg-v1",
        "name": "Example",
        "slug": "example",
        "description": "Example service",
        "tags": ["development"],
        "transport": "stdio",
        "mode": "lazy",
        "isolation": "service",
        "encoding": "utf-8",
        "startup_timeout": 30
      }
    }
  }
}
```

`config` 保存 MMG 元数据和其余运行设置；`format: "mmg-v1"` 明确区分此扩展与旧版 MMG 内部格式。MMG 通用 JSON 导入会合并顶层连接字段和扩展，旧格式继续兼容。默认导出隐藏敏感值；REST 映射属于 MMG 扩展，其他客户端不支持直接连接 REST 类型。基础连接字段参考 [Claude Code 官方 MCP 配置](https://code.claude.com/docs/en/mcp)。
