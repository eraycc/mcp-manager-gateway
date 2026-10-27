# 工具内嵌资源兼容与资源检索

[文档首页](../README.md) · [English README](../../README.md) · [中文 README](../../README.zh-CN.md) · [网关工具参考](../reference/gateway-tools.md)

## 为什么工具成功却只看到下载状态

某些 MCP 工具将文件正文放进标准的 `EmbeddedResource`（`type: "resource"`）块，而不是文本块。例如 GitHub MCP 的单文件 `get_file_contents` 可以同时返回下载状态文本和包含正文的 resource 块。客户端如果不展示 resource，用户可能只看到 SHA 或下载成功提示。

内嵌资源不一定出现在下游 `resources/list` 中，也不保证支持下游 `resources/read`。有些服务提供等价可读资源模板，例如 GitHub MCP 的 `repository_content`。此时优先发现资源模板，按返回的 `uriTemplate` 填充参数并保留 MMG 前缀，再通过原生 `resources/read` 或 `gateway_read_resource` 读取，无需先生成临时快照。是否存在可读模板与“HTTP 请求缺少参数镜像头”是独立问题。

MMG 提供默认关闭、适用于任何下游 MCP 的兜底兼容。客户端不支持 embedded resource 或上游未提供等价可读资源模板时，可开启以下措施：

1. 保留原始内容块、正文、二进制数据及 `structuredContent`。
2. 在有明确会话边界时，将内嵌资源保存为当前会话的临时快照。
3. 添加普通文本提示，给出实际可读的临时 URI；原 URI 不变。
4. 将快照加入当前会话的资源目录；原生列表及 `gateway_search_resources` 是否可用由各自独立开关控制。
5. 通过原生 `resources/read` 或 `gateway_read_resource` 直接读取快照，无需再次调用下游工具。

只缓存真正的内嵌资源，不自动抓取 `resource_link` 或任意 URL，也不根据 GitHub 服务名做特殊处理。

## 调用日志中的追加内容

网关在返回客户端前，将实际追加的内容块补记到同一条调用日志的 `gateway_processing.added_content`，例如：

~~~json
{
  "gateway_processing": {
    "added_content": [
      {
        "type": "text",
        "text": "Temporary embedded resource: mcp-manager://<server-id>/_embedded/<snapshot-id> ..."
      }
    ]
  }
}
~~~

`result` 保持原始上游结果；加工字段只记录新增提示，不重复保存原始内容块或资源正文，也不会新增业务调用次数。兼容关闭或没有追加内容时，不生成该字段。日志详情、下载和导出均保留加工信息，索引重建后也可读取。大响应继续使用现有载荷分离机制，完整加工信息应通过日志详情或下载查看。

## 资源兼容开关

在“系统设置 → 工具检索 → 资源兼容”中设置“启用内嵌资源注入”，对应系统设置 `embedded_resource.inject`，默认关闭。保存后应用于原生资源协议和网关资源工具。默认值用于未保存该设置的环境；已有明确保存的开关值继续生效。

关闭时停止捕获、不写入临时缓存，并立即清空当前 MMG 进程内所有会话与租约的临时快照。工具响应原样透传、不追加临时读取提示，旧临时 URI 不再可读。重新开启不会恢复已清空的快照，必须重新调用工具生成。进行中的响应也不能在关闭清空之后重新写回旧快照。静态资源、提示词和资源模板继续正常提供。

## 原生资源列表开关

同一“资源兼容”卡片中的“对外暴露原生资源”对应系统设置 `exposeNativeResources`，默认开启，与内嵌资源注入一起保存。

关闭后，原生 `resources/list`、`prompts/list`、`resources/templates/list` 返回空列表且不再分页，临时资源也不出现在原生列表中。它只隐藏索引，不改变资源数据、权限、内嵌资源捕获或已生成的快照；重新开启后，仍有效的条目恢复可见。已知 URI 的 `resources/read` 和已知提示词名称的 `prompts/get` 仍按既有权限正常工作。

访问令牌的“启用资源工具”独立决定是否提供 `gateway_search_resources` 与 `gateway_read_resource`。原生列表开关不启用、不禁用、不改变这两个工具；关闭原生列表后，需要按需发现资源的令牌可单独开启资源工具。两个开关均关闭时无法通过列表或搜索发现资源，但已知标识的原生读取仍可用。

保存后若原生列表开关实际发生变化，已连接客户端会通过 MCP 的列表变更通知机制收到资源和提示词刷新通知；资源模板随资源通知刷新。现代协议客户端需订阅相应列表事件。

## 正确的读取流程

先按标准流程发现工具并执行，随后使用调用结果中的实际临时 URI，或查询：

~~~json
{
  "mcp": "example-service",
  "kind": "resource",
  "uri_prefix": "repo://",
  "limit": 20
}
~~~

将上述参数传给 `gateway_search_resources`。同一会话或租约内，匹配 MCP 的临时资源排在注册资源之前，且最新生成的条目永远优先；原生 `resources/list` 也采用此顺序。超过一页时仍需跟随游标。返回项包含 `temporary: true`、原始 `source_uri` 和 `read_with`；直接执行 `read_with` 指定的读取操作即可。不要自己拼接临时 URI 或网关前缀。

`keyword="admin/layout.tsx"`、`keyword="layout.tsx"` 均可按 `source_uri` 子串命中；`keyword="admin layout tsx"` 支持分词匹配。临时正文不参与全文或向量检索，兼容开关也不会改变管理界面的持久能力计数。

临时 URI 形如 `mcp-manager://<server-id>/_embedded/<opaque-id>`。每次调用产生独立快照，即使下游返回同一原始 URI，也不会覆盖之前的调用结果或静态资源。

## 会话、权限与容量

- 旧版有状态 HTTP 客户端按 MMG 验证过的 `Mcp-Session-Id` 隔离。
- 显式租约客户端通过 `POST /gateway/v1/leases` 取得租约，在调用、列表和读取请求中持续携带 `X-MCP-Manager-Lease`；MMG stdio Bridge 自动管理自己的租约。
- 已认证请求没有显式会话或租约头时，复用 MMG 已为该身份建立的隐式 `retention` 运行租约作为缓存作用域，后续使用同一认证身份的请求可以发现和读取它的快照。相同 Token 的无状态请求共享该隐式租约；需要独立隔离的 Agent 应使用独立 Token 或显式租约。
- 资源只对产生它的会话或租约可见；显式会话或租约之间不会因为 Token 相同而互相读取快照。已进入显式会话的客户端应持续携带其标识，不会按 Token 猜测丢失的会话。
- 匿名无状态请求必须携带稳定的 `X-MCP-Manager-Client`，或使用有效会话/租约。完全没有稳定身份时保留原始内容并明确提示无法缓存，不静默丢弃。
- 每次列出和读取都检查当前服务权限、启用状态、配置版本及凭据作用域。凭据或配置变化后，旧快照不能继续读取。
- 快照仅保存在当前进程内存中，不写数据库、持久能力目录或向量索引，不发送给 embedding 服务。
- 快照最长保留 30 分钟；会话关闭、租约释放或过期、Token 撤销以及进程重启也会使快照失效。
- 单个资源上限为序列化后的 4 MiB，每会话最多 128 个、16 MiB；全进程最多 4096 个、64 MiB。超限时淘汰最早快照；单项超限会在文本提示中说明，原始响应块仍保留。
- 多进程部署必须将同一会话路由回原进程；临时快照不跨进程同步。

目录变化或快照过期后，旧分页游标会失效；重新从第一页查询。读取过期或无权访问的临时 URI 会返回明确错误，不会将它转发给下游碰运气。

## 静态资源检索

`gateway_search_resources` 支持：

| 参数 | 行为 |
| --- | --- |
| `kind` | `resource`、`prompt`、`template` 或默认 `all` |
| `match` | 默认 `fuzzy`；也支持 `exact`、`keyword_only` |
| `keyword` | 查询词；为空时分页遍历 |
| `uri_prefix` | 按下游原始 URI 或 URI 模板前缀筛选；临时资源按 `source_uri` 筛选，提示词不匹配该过滤 |
| `mcp`、`limit`、`cursor` | 服务筛选与稳定分页 |

`exact` 对名称进行区分大小写的完整匹配；`keyword_only` 仅使用关键词，不调用 embedding；`fuzzy` 融合关键词、有限拼写容错及可选向量相似度。

静态资源、提示词和模板复用后台向量构建与受授权范围限制的检索模块。只对描述性目录字段建立向量，不索引正文、原始 URI 或扩展字段。交互查询只计算查询向量，不同步生成整个目录的向量。语义候选最多 200 个，由关键词命中和有界的目录补充窗口组成；大目录可先用 `mcp`、`kind`、`uri_prefix` 缩小范围，或使用无关键词分页遍历完整目录。

成功的向量融合检索报告 `search.mode: "hybrid"`、`semantic_status: "ready"`。未开启模型、索引尚未构建或模型失败时保留关键词检索，并报告 `disabled` 或 `unavailable` 及具体原因，例如 `cold_index`、`timeout`；不伪装成语义成功。空查询或精确匹配无需语义检索。临时快照仅做本地匹配，始终不参与 embedding。

## HTTP 参数镜像

协商到适用的 MCP HTTP 协议版本后，MMG 根据当前已授权工具 Schema 的 `x-mcp-header` 标注生成对应的 `Mcp-Param-*` 请求头。头名后缀取自标注值，值由 SDK 按协议编码，支持嵌套属性及需要 Base64 包装的 Unicode、换行等内容。

镜像发生在共享下游 HTTP 转发层，覆盖管理界面手动调测和正常网关调用。它只修改本次 HTTP 请求，不把工具参数写入共享默认头，不需要先重新枚举整份下游目录，也不会在调用失败后自动重放。

参考：[MCP Python SDK 参数镜像说明](https://py.sdk.modelcontextprotocol.io/advanced/header-parameters/)、[OpenCode 内嵌资源兼容问题](https://github.com/anomalyco/opencode/issues/7878)。
