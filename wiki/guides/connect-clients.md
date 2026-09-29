# 连接 Agent 与 MCP 客户端

MMG 对外提供一个统一网关。每个 Agent 只需要保存这一条连接，不必重复配置全部下游 MCP。

## Streamable HTTP

统一端点：

~~~text
http://127.0.0.1:8765/mcp
~~~

使用 Bearer Token：

~~~http
Authorization: Bearer <MCP_MANAGER_TOKEN>
~~~

支持 MCP 会话的客户端可在退出时发送 DELETE 释放会话。通用 HTTP 客户端由网关维护保留租约并按空闲策略回收实例。

## stdio 桥接

网关必须已经运行。桥接只代理到 MMG，不在本地重复运行下游服务。

~~~console
mmg stdio --url http://127.0.0.1:8765 --token <MCP_MANAGER_TOKEN>
~~~

推荐通过环境变量传递 Token：

### PowerShell

~~~powershell
$env:MCP_MANAGER_TOKEN = "<MCP_MANAGER_TOKEN>"
mmg stdio --url http://127.0.0.1:8765
~~~

### POSIX Shell

~~~sh
MCP_MANAGER_TOKEN="<MCP_MANAGER_TOKEN>" mmg stdio --url http://127.0.0.1:8765
~~~

也可以设置 `MCP_MANAGER_URL`，省略 `--url`。

通用客户端配置：

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

## discovery 与 native

| 模式 | 客户端看到的工具 | 适用场景 |
| --- | --- | --- |
| `discovery` | 少量搜索、调用和可选扩展工具 | MCP 较多，希望减少启动与上下文成本 |
| `native` | 当前权限内的全部下游工具 | 工具较少，或客户端依赖原生工具目录 |

推荐 `discovery`。无论哪种模式，实际调用都继续经过 Token 范围、用户授权、工具过滤、OAuth 和运行时隔离检查。

## 远程访问

默认只监听本机。需要从其他设备访问时：

~~~dotenv
HOST=0.0.0.0
PUBLIC_URL=https://mcp.example.com
COOKIE_SECURE=true
~~~

生产环境建议：

- 使用 HTTPS 反向代理；
- 防火墙只开放必要来源；
- 设置允许的 Host/IP；
- 将跨域来源从 `*` 收紧为客户端真实来源；
- 不在 URL 查询参数中携带 Token。

`PUBLIC_URL` 用于 OAuth 回调，也是反向代理场景的可信来源。改变它后需要同步更新 OAuth 提供方登记的回调地址。

## 连接检查

1. `mmg --version` 能正常执行；
2. 浏览器能打开控制台；
3. Token 未禁用、未过期；
4. Token 已分配目标 MCP；
5. 客户端地址包含 `/mcp`；
6. stdio 桥接的 stdout 没有混入普通日志；
7. OAuth 服务已经由当前 Token 所属用户授权。
