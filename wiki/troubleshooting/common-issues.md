# 常见问题

## Agent 看不到目标 MCP

检查：

1. 服务不是 `disabled`；
2. 用户已被分配该服务；
3. Token 范围包含该服务；
4. OAuth 用户已完成授权；
5. 工具没有被过滤规则禁用；
6. 目录刷新没有失败。

先调用 `gateway_search_mcps`。不存在与 `failed` 是不同情况。

## 搜索能看到服务，但没有工具

查看 `catalog_status` 和最近刷新错误。可能原因：

- 下游离线；
- 环境变量缺失；
- OAuth 过期；
- 配置修改后尚未成功发现；
- 服务确实返回空工具集；
- 当前用户的个人目录尚未建立。

不要通过反复搜索来启动 `lazy` 服务；应在控制台修复配置并刷新。

## `gateway_call` 参数错误

重新调用 `gateway_search_tools`，使用返回的完整 `inputSchema`。重点检查：

- required 字段；
- 嵌套对象与数组；
- enum；
- 数字与字符串类型；
- `additionalProperties`；
- `anyOf` / `oneOf`。

不要猜测旧 Schema，也不要把 arguments 传成 JSON 字符串。

## stdio 桥接无法连接

检查：

- HTTP 网关已启动；
- URL 含正确端口；
- Token 未过期；
- `MCP_MANAGER_TOKEN` 已传给桥接进程；
- 客户端启动的 `mmg` 来自正确安装环境；
- stdout 没有普通日志破坏 MCP 协议。

## OAuth 回调失败

检查 `PUBLIC_URL` 是否与浏览器实际访问的 HTTPS 地址及 OAuth 提供方登记地址一致。反向代理部署时设置 `COOKIE_SECURE=true`。

一位用户授权失败不应导致其他用户的服务被禁用。

## 服务反复进入 failed

查看启动失败次数和最近原因。常见问题：

- command 不存在；
- 子进程依赖未安装；
- 工作目录或文件权限错误；
- 远程地址不可达；
- 必需环境变量缺失；
- 请求头或凭据无效。

修复后显式启动或恢复运行策略，再执行刷新和代表性测试。

## 调用返回 `outcome_unknown`

请求可能已到达下游，但结果没有返回。不要自动重试写操作。先检查目标系统是否已经发生变更，再由人决定后续动作。

## 升级后版本没有变化

先确认安装方式和命令来源：

~~~console
mmg --version
where mmg
~~~

POSIX 环境可使用 `command -v mmg`。

- uv tool：使用 `uv tool upgrade mcp-manager-gateway`；
- pip：在正确虚拟环境执行 `pip install --upgrade mcp-manager-gateway`；
- 源码：拉取代码并 `uv sync`；
- Docker：重建或拉取镜像并重建容器。

`mmg upgrade` 只迁移数据库，不更新程序版本。
