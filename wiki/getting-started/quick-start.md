# 快速开始

## 1. 启动服务

~~~console
mmg
~~~

默认地址为 <http://127.0.0.1:8765>。也可以显式指定端口：

~~~console
mmg serve --port 8766
~~~

首次启动会自动创建数据目录、配置文件、数据库和根密钥。

## 2. 创建管理员

在浏览器打开控制台并注册。第一个账户自动成为管理员。

管理员负责：

- 添加、导入和测试 MCP；
- 决定运行策略和实例隔离；
- 为用户分配 MCP；
- 创建具备管理能力的 Token；
- 查看运行状态、任务和审计信息。

## 3. 添加第一个 MCP

进入“MCP 服务”，选择手工添加或导入配置。填写 transport 和连接配置后，先运行诊断或工具测试。

常见 transport：

| 类型 | 用途 |
| --- | --- |
| `stdio` | 启动本地子进程，通过标准输入输出通信 |
| `streamable-http` | 连接远程 Streamable HTTP MCP |
| `sse` | 兼容旧 SSE MCP |
| `rest` | 将 HTTP REST 接口映射为 MCP 工具 |

建议首次使用 `lazy`：发现目录时不启动下游，第一次真实调用才启动。

## 4. 创建访问 Token

在个人中心创建 Token，并选择：

- 可访问的 MCP 范围；
- `discovery` 或 `native` 工具模式；
- 是否启用资源工具；
- 管理员是否启用 MCP 提议工具。

推荐新 Token 使用 `discovery`。它只注册少量网关工具，避免把所有下游 Schema 一次塞入 Agent 上下文。

Token 只在安全位置保存。不要把真实值写进代码仓库、截图或提议内容。

## 5. 连接客户端

支持 Streamable HTTP 的客户端连接：

~~~text
URL: http://127.0.0.1:8765/mcp
Authorization: Bearer <MCP_MANAGER_TOKEN>
~~~

只支持本地 stdio 的客户端使用桥接：

~~~json
{
  "mcpServers": {
    "mcp-manager": {
      "command": "mmg",
      "args": ["stdio", "--url", "http://127.0.0.1:8765"],
      "env": {
        "MCP_MANAGER_TOKEN": "<MCP_MANAGER_TOKEN>"
      }
    }
  }
}
~~~

控制台个人中心可以生成适合当前地址的配置。更多示例见 [连接客户端](../guides/connect-clients.md)。

## 6. 验证渐进式调用

让 Agent：

1. 使用 `gateway_search_mcps` 列出可访问服务；
2. 使用 `gateway_search_tools` 获取目标工具的完整 Schema；
3. 使用返回的精确 `gateway_name` 调用 `gateway_call`。

如果服务是 `lazy`，前两步不会启动下游，第三步才会按需启动。

下一步：

- [迁移已有 MCP](../guides/migrate-mcps.md)
- [服务管理与测试](../guides/manage-services.md)
- [渐进式发现原理](../concepts/progressive-discovery.md)
