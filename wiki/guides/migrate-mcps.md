# 迁移已有 MCP

迁移的目标不是把配置换个位置，而是让多个 Agent 以后只连接 MMG，由网关统一管理发现、权限、凭据和生命周期。

## 两种迁移方式

### Web 控制台导入

适合管理员直接操作。支持：

- 通用 `mcpServers` / `mcp_servers` JSON；
- Codex `config.toml`；
- Claude 配置；
- DSH registry 与 Cordis 配置；
- 单服务或服务数组 JSON。

流程：

1. 上传、粘贴或从网关主机的默认位置只读扫描；
2. 解析并检查服务列表；
3. 编辑或删除不需要的条目；
4. 运行真实诊断；
5. 去重并选择冲突策略；
6. 导入并观察目录刷新任务。

扫描读取的是运行 MMG 的主机。浏览器在另一台设备时，应上传文件或粘贴经过检查的内容。

### Agent 批量提议（推荐）

适合已有大量 MCP 的用户。管理员为受信任 Token 临时开启“MCP 提议”，Agent 读取现有配置并调用 `gateway_mcp_proposals`。

~~~json
{
  "proposals": [
    {
      "name": "Example Filesystem",
      "slug": "example-filesystem",
      "description": "访问项目工作目录",
      "tags": ["filesystem", "development"],
      "transport": "stdio",
      "config": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
      },
      "source": "existing-client-config",
      "purpose": "迁移现有开发工具",
      "declared_capabilities": ["读取和管理项目文件"],
      "requested_permissions": ["访问 /workspace"]
    }
  ]
}
~~~

单批支持 1–100 条。提议不会直接创建服务，也不会启动命令。

## 审批者必须决定的内容

Agent 不应在提议中指定以下审批字段：

- `mode`：`lazy` / `eager` / `disabled`；
- `isolation`：服务、用户或会话实例；
- `config_isolation`：OAuth 或凭据配置隔离。

审批者应检查：

- 可执行命令和参数；
- 工作目录、文件系统范围；
- URL、请求头和网络目标；
- 环境变量名以及密钥注入方式；
- 服务是否会写入或删除业务数据；
- 需要的 OAuth scopes；
- 工具测试是否安全、可重复。

## 配置兼容规则

- 导入别名 `http` 会规范化为 `streamable-http`；
- 环境变量引用在网关运行环境中解析，缺失时明确报错；
- Codex 工具启用/禁用限制会保留；
- 客户端已有 OAuth 登录缓存不会迁移，需要在 MMG 中重新授权；
- 同一地址使用不同凭据的服务不能简单视为重复；
- 导入默认使用 `lazy`，来源明确禁用时继续禁用。

## 批准与验证

批准时 MMG 再次校验审批后的配置，然后创建服务并刷新能力目录。Agent 应查询提议状态，并在批准后执行：

1. `gateway_search_mcps` 确认服务可见；
2. `gateway_search_tools` 确认工具与 Schema；
3. 选择无副作用或风险可控的工具做代表性调用；
4. 报告每个服务的结果，不能用部分成功代表全部成功。

## 清理旧配置

只有同时满足以下条件才能删除旧条目：

- 用户明确批准清理；
- 所有目标提议已批准；
- 所有目标服务均可发现；
- 已完成适当的真实调用验证；
- 已创建可恢复备份；
- 能精确定位对应条目。

只删除已迁移的 MCP 条目，保留无关设置和 MMG 自身连接。不要删除原配置文件、数据库、密钥或数据目录。

建议迁移完成后撤销 Token 的提议权限或轮换 Token。
