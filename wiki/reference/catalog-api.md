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

快照只供运行该测试的管理员访问，绑定提议 revision 和 run ID，24 小时后失效。审批通过时，在同一事务内删除该提议的测试目录条目，保留测试结果摘要；共享定义文件仍按引用关系由存储清理流程回收。已通过、已拒绝记录不显示“能力目录”按钮；已通过服务的当前能力应从 MCP 服务列表查看。修改、重新测试、批准或拒绝会使旧快照失效；它不是已批准的 MCP，不可作为网关调用入口。编辑时附上已读取的 `revision`，过期编辑返回 409，需要重新加载。

## 兼容与大小限制

服务目录与审批诊断共用 8 KiB 元数据摘要策略。小型元数据保持原样；超限时先省略 `serverInfo.icons`，不足以缩小时再提取服务名称、版本、标题、说明、网址、协议版本和能力错误。文本按实际序列化 JSON 的 UTF-8 字节数裁剪，网址超长时省略而不生成错误链接，未知扩展不进入紧凑摘要。错误列表优先保留 tools、resources、prompts、templates 四类失败，并通过 `capability_errors_total` 与 `capability_errors_truncated` 说明其余错误数量及裁剪情况。

`metadata_truncated`、`metadata_truncated_fields` 和 `metadata_omitted_fields` 明确标记摘要变化。这类展示元数据超限不会再阻塞刷新或审批测试，完整工具定义及 inputSchema 独立保存，不随元数据摘要裁剪。完整诊断响应及传输总量仍遵循各自的安全预算。原始元数据计入 `discovery_max_message_bytes` 和 `discovery_max_decoded_bytes`，不会额外占用能力分页页数；达到原始发现预算时仍返回 `discovery_budget_exceeded`。此策略不会解除原始传输或完整定义的安全限制。

旧 `GET /api/v1/mcps/{id}/tools` 只读取缓存，不自动连接。旧完整目录测试、诊断和刷新响应限制为 1 MiB；服务与提议列表页限制为 128 KiB。超限返回明确的 `catalog_payload_too_large`，不截断 inputSchema，也不返回大块 JSON 让浏览器处理。

大目录集成应使用摘要分页和单条详情；服务/提议列表遇到超限时减小 `page_size`。旧测试响应默认仍兼容小目录；需要大目录诊断时显式使用 `summary=true`。现有目录缓存的显式升级导入另见[备份与迁移](../operations/backup-and-migration.md)。

## MCP protocol pagination

`gateway_search_tools` accepts optional `names_only: true` for complete name traversal without reading definition artifacts. Obtain the original full `inputSchema` through exact search before `gateway_call`. Providers in each response belong only to that page. Follow opaque `next_cursor` with unchanged query and page size; identity, permission, credentials, generation, feature or retrieval changes invalidate it. Native lists use `nextCursor`. `gateway_list_resources` accepts `limit` and `cursor`, returns summary items and a total. Whole JSON-RPC responses include all text and structured representations in the 1 MiB budget. Individual oversized contracts fail explicitly with `catalog_entry_too_large`.

## Console cache and full detail display

检测到旧版缓存时，目录响应带有 `refresh_required: true`，控制台提示管理员刷新。刷新成功后，当前弹窗直接显示新目录。新增、导入、审批通过与复制服务后，都会自动尝试建立同一份分页能力目录；连接失败时服务仍保留，并展示可操作的缓存错误。服务列表和个人资料中的工具、资源、提示词、资源模板数量来自当前用户作用域的已发布目录。已禁用服务保持禁用；需要个人凭据或 OAuth 授权的服务在授权完成后刷新。审批中已有成功测试快照时，“能力测试”先提供查看缓存或重新测试的选择；快照过期时需要重新测试。

MCP 服务的“能力/测试”、MCP 审批的“能力目录”和个人资料的“查看能力”共用能力卡片；长描述、URL 和连续标识符在桌面与手机上均在卡片内换行。

能力列表保持摘要分页。用户显式打开单项完整定义或执行单工具测试时，控制台显示完整响应，通过后台格式化和分批更新保持界面响应；服务器端传输与存储大小预算仍然有效，超预算返回明确错误而非成功截断。

## 定时与手动刷新

定时刷新使用系统设置中的 Cron 表达式和时区。例如 `0 3 * * *` 配合 `Asia/Shanghai` 表示每天北京时间 03:00。启用后按后续触发时间执行，不补跑网关停机期间错过的时间点。

定时任务跳过 `mode=disabled` 的服务；按需和常驻服务即使已手动停止或启动失败，也会尝试重新连接、发现能力并发布分页目录。用户隔离服务仍仅刷新已获授权的凭据所有者作用域，缺少凭据时不会绕过鉴权。手动刷新使用相同的分页目录发布路径。刷新成功后，服务列表的缓存时间和能力目录一并更新。

“停止”仅强制关闭该 MCP 当前运行的实例，不修改运行策略，不阻止后续业务调用或定时刷新。后续调用会重新启动按需或常驻服务；按需服务仍遵循空闲回收规则，常驻服务启动后保持运行。停止可能中断正在执行的调用，结果未知时不得自动重放。要持续阻止启动，应将运行策略设为禁用。

刷新失败时立即使对应作用域的目录失效，工具列表和数量置空，记录失败状态、错误码、原因和时间到数据库，并向 Agent 与控制台暴露；不回退旧缓存。失败的运行态只用于诊断，不改变按需/常驻策略，也不作为调用拒绝条件。停止链路不设置持续阻断标记，只有运行策略 disabled 才停用服务。

目录搜索与列表查询始终只读，返回空工具列表及失败原因，提示管理员自检；不会隐式连接下游。已授权的显式精确调用遇到失效目录时，可重新发现并校验当前工具定义，再执行一次业务调用；不会使用旧 Schema，也不会自动重放结果未知的业务操作。

## 个人能力缓存刷新

`GET /api/v1/mcps/{id}/credentials/status` 返回 `revision`、`refresh_supported`、`refresh_allowed` 和 `refresh_reason`。管理员和普通用户均可调用 `POST /api/v1/mcps/{id}/credentials/refresh`，JSON 请求体仅为 `{"revision": 1}`；使用刚读取的服务版本，不接受用户 ID 或其他作用域覆盖参数。

该接口仅支持按用户隔离且需要认证的 Streamable HTTP、SSE、REST 服务，要求已保存本人的完整凭据，OAuth 还需有效授权。共享 OAuth 配置由管理员维护，但每位用户仍只刷新自己的授权作用域。禁用服务不能刷新，过期 revision 返回 409；撤销访问、删除服务、修改隔离或凭据后，过期结果不能发布。成功响应返回缓存状态、时间和四类能力数量，不返回完整能力定义。发现失败清空本人目录，记录错误码和原因；修复后可以重试。

管理员的 `GET /api/v1/mcps` 列表对按用户隔离的认证服务附带 `personal_credentials`：`current_user_configured`、`configured_users`、`total_users`。只统计有权访问该 MCP 的启用用户（包含管理员）；配置完成要求凭据完整保存，OAuth 还需未过期或可续期授权，与能力发现是否成功无关。普通用户列表不包含这些统计，不返回其他用户的身份或凭据。控制台显示当前管理员自己的能力缓存。

## Service configuration export

控制台 MCP 服务页的“导出配置”经确认后调用此接口，不传 `ids`，导出全部服务；批量“导出”传入跨页选中的 `ids`。两者均使用 `include_secrets: false`，敏感字段脱敏，重新导入后需补齐凭据。

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
