# 网关工具参考

discovery Token 默认提供三个核心工具。资源与提议工具按 Token 权限额外启用。

## `gateway_search_mcps`

搜索服务或列出全部授权服务。

主要参数：

| 参数 | 说明 |
| --- | --- |
| `query` | 空或 `*` 表示全部；其他值搜索名称、slug、描述和标签 |
| `limit` | 每页数量 |
| `cursor` | 下一页游标 |

结果包括服务 ID、名称、slug、描述、标签、工具数量、目录状态和下一步可用的 `mcp` 值。不会返回连接配置或凭据。

## `gateway_search_tools`

搜索工具并返回完整定义。

| `mcp` | `tool` | 行为 |
| --- | --- | --- |
| 指定服务 | 空或 `*` | 列出该服务全部工具 |
| 指定服务 | 查询词 | 服务内搜索 |
| 空或 `*` | 查询词 | 跨授权服务搜索 |
| 空或 `*` | 空或 `*` | 分页遍历完整授权目录 |

每项提供精确 `gateway_name`、服务归属、描述、完整 `inputSchema` 和调用说明。服务名称歧义时不会暗中选择一个；未找到指定服务时不会扩大成全局搜索。

## `gateway_call`

~~~json
{
  "name": "example__tool",
  "arguments": {
    "required_field": "value"
  }
}
~~~

`name` 必须来自搜索结果，`arguments` 必须是对象并符合原始 Schema。执行前重新检查权限、配置版本和参数。

错误可能提供字段路径、期望类型和恢复建议，但不会泄露无权访问的服务或秘密配置。

## `gateway_list_resources`

可选工具，为客户端列出：

- resources；
- resource templates；
- prompts。

可以按服务和关键词筛选。返回提供方、精确 URI 和后续读取方式。

## `gateway_read_resource`

使用列表结果中的精确 URI 读取资源。该入口是只读资源协议适配，不等价于执行任意业务工具。

## `gateway_mcp_proposals`

仅对启用提议权限的管理员 Token 可见。

单条提交使用提议字段；批量提交使用 `proposals` 数组，最多 100 条。查询状态：

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
