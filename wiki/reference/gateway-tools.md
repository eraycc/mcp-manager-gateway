# 网关工具参考

discovery Token 默认提供三个核心工具。资源与提议工具按 Token 权限额外启用。

工具列表保留简短用途和必要约束，参数说明各不超过 10 个英文词；每个工具的 `inputSchema.examples` 提供一条最小调用示例。下表均为工具参数，尖括号值必须替换为搜索返回的精确值；`gateway_call.arguments` 应根据实际下游 Schema 填写。

| 工具 | 最小参数示例 |
| --- | --- |
| `gateway_search_mcps` | `{"query":"files"}` |
| `gateway_search_tools` | `{"mcp":"<service-id>","tool":"<tool-name>"}` |
| `gateway_call` | `{"name":"<gateway_name>","arguments":{}}` |
| `gateway_search_resources` | `{"keyword":"config","kind":"resource"}` |
| `gateway_read_resource` | `{"uri":"<returned-uri>"}` |
| `gateway_mcp_proposals` | `{"name":"Example","transport":"streamable-http","config":{"url":"https://mcp.example/mcp","auth":{"type":"oauth","auth_mode":"cimd_only"}}}` |

## `gateway_search_mcps`

搜索服务或列出全部授权服务。

主要参数：

| 参数 | 说明 |
| --- | --- |
| `query` | 空或 `*` 表示全部；其他值搜索名称、slug、描述和标签 |
| `limit` | 每页 1–200 条，默认 50 条 |
| `cursor` | 下一页游标 |

结果包括服务 ID、名称、slug、描述、标签、工具数量、目录状态和下一步可用的 `mcp` 值。不会返回连接配置或凭据。

`tools_list` 是最多 50 个名称的有界预览，不等于完整目录。出现 `tools_list_truncated` 时，使用该服务 ID 调用 `gateway_search_tools` 并跟随 cursor 枚举完整定义。空缓存返回明确状态，搜索不会为它启动下游。

## `gateway_search_tools`

搜索工具并返回完整定义。

| `mcp` | `tool` | 行为 |
| --- | --- | --- |
| 指定服务 | 空或 `*` | 列出该服务全部工具 |
| 指定服务 | 查询词 | 服务内搜索 |
| 空或 `*` | 查询词 | 跨授权服务搜索 |
| 空或 `*` | 空或 `*` | 分页遍历完整授权目录 |

工具项提供精确 `gateway_name`、完整 `inputSchema`，以及存在时的描述、输出 Schema、示例和调用模板；提供方在响应的 `mcps` 中。查询先选轻量候选，再读取返回页的完整定义，不提前构造全目录契约。服务名称歧义时不会暗中选择一个；未找到指定服务时不会扩大成全局搜索。

## `gateway_call`

~~~json
{
  "name": "example__tool",
  "arguments": {
    "required_field": "value"
  }
}
~~~

`name` 必须来自搜索结果，`arguments` 必须是对象并符合原始 Schema。精确名称通过索引定位目标工具，不使用模糊路由或启动其他服务。执行前重新检查权限、凭据、配置版本和当前 Schema；校验器按 Schema 哈希有界复用。

错误可能提供字段路径、期望类型和恢复建议，但不会泄露无权访问的服务或秘密配置。

Schema 校验只解析自包含定义中的本地引用，不自动读取远程 URL 或本机文件。无法本地解析的引用返回 `schema_reference_unavailable`；服务应提供完整 `$defs`。披露的原始 Schema 不会因此被删改。

## `gateway_search_resources`

可选的渐进式资源搜索工具，与 `gateway_search_mcps`、`gateway_search_tools` 一样按需披露授权目录元数据，搜索对象包括：

- resources；
- resource templates；
- prompts。

支持 `mcp`、`keyword`、`kind`（`resource` / `prompt` / `template` / `all`）、`match`（`exact` / 默认 `fuzzy` / `keyword_only`）及下游原始 `uri_prefix`。精确匹配区分名称大小写；关键词模式跳过 embedding；模糊模式使用有界候选和向量融合，成功状态为 `hybrid` / `ready`，失败时明确报告关键词降级原因。静态资源向量在后台构建；查询不启动下游服务。

系统设置 `embedded_resource.inject` 默认关闭。开启时合并当前会话或租约的工具内嵌资源快照，临时资源先于静态条目、按生成时间倒序显示，返回提供方、精确 URI、`temporary` 标记及后续读取方式。`keyword` 支持 `source_uri` 子串和分词匹配，例如 `admin/layout.tsx`、`layout.tsx` 或 `admin layout tsx`。已认证无状态请求复用当前隐式 retention 租约，不因缺少显式会话头静默漏掉新快照。关闭时停止捕获并清空全部临时快照，旧 URI 不再可读，工具响应原样透传；重新开启后必须重新调用工具。临时资源不参与向量索引。会话要求、容量、GitHub MCP 示例与 HTTP 参数镜像见[内嵌资源兼容与检索](../guides/embedded-resources.md)。

创建或编辑 Token 时，勾选“启用资源工具”会同时开放 `gateway_search_resources` 和 `gateway_read_resource`；未勾选时两者均不披露。已勾选的 Token 无需重建。升级后客户端应重启或重连，刷新工具目录后使用新名称。

不传参数（`{}`）时，仅返回授权目录的第一页，默认最多 **20 条**元数据，涵盖资源、提示词和模板；`limit` 范围为 **1–100**，没有一次性返回全部条目的选项。响应提供 `total`、`returned` 与 `next_cursor`。继续读取时保持筛选条件与 `limit` 不变，仅追加返回的 `cursor`；空参数首查可用 `{"cursor": "<next_cursor>"}` 续页。目录、权限或查询条件变化后游标失效，客户端应重新搜索，不能沿用旧游标。

搜索仅返回当前页元数据与 `read_with` 等读取提示，不预加载资源正文；需要内容时再调用读取工具，保持渐进式披露。单页还受响应字节上限约束，实际数量可能少于 `limit`，应以 `next_cursor` 判断是否还有下一页。

## `gateway_read_resource`

使用列表结果中的精确 URI 读取资源或提示词。该入口是只读资源/提示词协议适配，不等价于执行任意业务工具。

提示词项提供 `uri`、`read_with` 和原生 `prompts/get` 的 `get_with`。兼容 URI 形如 `mcp-manager://<server-id>/prompt://example/workflow`，应直接使用列表返回值，不自行猜测名称。读取时网关精确查找授权提示词并调用下游 `prompts/get`，不会将资源读取失败后盲目重试为提示词。真实声明的同 URI 资源优先按资源处理。

~~~json
{
  "uri": "mcp-manager://<server-id>/prompt://example/workflow",
  "arguments": {"topic": "example"}
}
~~~

`arguments` 只用于提示词，参数值必须是字符串；读取普通资源时应省略，非空参数会被拒绝。缺少必填参数或参数名错误时返回 `invalid_arguments`，并说明具体字段；不启动下游。工具响应保留完整 `messages`，同时在 `contents` 中提供完整提示词结果的 JSON。原生 `resources/read` 也可读取无需参数的兼容提示词 URI；需要参数时使用原生 `prompts/get` 或上述工具入口。

## `gateway_mcp_proposals`

仅对启用提议权限的管理员 Token 可见。

单条提交使用提议字段；批量提交使用 `proposals` 数组，最多 100 条。OAuth 配置放在 `config.auth`，设置 `type: "oauth"`，`auth_mode` 支持 `pre_registration`、`prefer_cimd_fallback_dcr`、`cimd_only`、`dcr_only`；预注册模式需要 `client_id`。各模式前提、CIMD 元数据和 DCR 回退规则见 [OAuth 指南](../guides/oauth.md)。`mode`、`isolation`、`config_isolation` 仍由审批人设置。查询状态：

~~~json
{
  "action": "list",
  "status": "pending",
  "page": 1,
  "page_size": 50
}
~~~

状态包括 `pending`、`incomplete`、`approved`、`rejected`。提议不能设置 `mode`、`isolation`、`config_isolation` 等审批字段。

## 分页规则

- 跟随 `next_cursor`；
- 查询参数保持不变；
- 目录变化后旧游标可能返回 `catalog_changed`；
- 单个工具定义不会跨页拆分；
- 超大目录条目会返回明确错误而不是静默删字段。

## 调用原则

可靠 Agent 应始终：

1. 搜索服务；
2. 搜索工具；
3. 读取完整 Schema；
4. 使用精确名称；
5. 校验参数；
6. 对副作用工具避免自动重试。

## 失败目录、拼写容错与旧入口

服务达到启动失败阈值后，服务搜索继续报告 `failed` 状态、失败次数与原因，但工具数量为零、名称预览为空；工具搜索和原生目录列表不会将旧索引中的能力作为可用工具披露。已知工具的调用仍返回明确的 `mcp_failed`，不是空成功。

关键词检索首先查询精确词项，未命中时对长度 4–32 的 ASCII 单词尝试单字符删除、插入、替换或相邻交换。候选哈希总数最多 4096，结果候选最多 200；只查询授权目录的现有词项索引，不扫描或读取全目录 Schema。此容错仅用于发现；`gateway_call` 与精确检查始终使用准确名称。

旧 `gateway_search` 和 `gateway_inspect` 入口继续兼容，但读取新版索引。`gateway_search` 的文本结果仍为名称摘要数组，支持 `query`、`limit`、`cursor`，默认每页 50；结构化结果包含 `items`、`total`、`next_cursor`。续页时保持原查询和 limit 不变。`gateway_inspect` 用精确 `name` 返回完整单工具 Schema，不启动下游，也不把无前缀或拼写近似的名称路由到工具。新客户端优先使用上述三个核心发现与调用工具。
