# 传输插件

传输插件通过 Python entry point 扩展 MMG，不需要修改网关核心。插件安装在运行 MMG 的同一个 Python 环境中。

## 注册

~~~toml
[project.entry-points."mcp_manager.transports"]
custom-service = "my_transport:plugin"
~~~

插件名称不能覆盖内置 `stdio`、`streamable-http`、`sse` 或 `rest`。

## 接口

~~~python
from contextlib import asynccontextmanager

class Plugin:
    def validate(self, config):
        if not config.get("url"):
            raise ValueError("url required")

    @asynccontextmanager
    async def connect(self, spec):
        connection = await open_connection(spec.config)
        try:
            yield connection
        finally:
            await connection.close()

plugin = Plugin()
~~~

连接对象应提供：

- `async discover()`：返回 tools、resources、prompts、templates；
- `async call(name, arguments)`：返回 MCP `CallToolResult` 的 JSON wire 对象；
- 可选 `async read_resource(uri)`；
- 可选 `async get_prompt(name, arguments)`。

工具至少包含 `name` 和完整 `inputSchema`。

## 生命周期要求

- `connect` 的进入与退出由同一个运行时任务管理；
- 关闭逻辑必须支持取消并确保资源释放；
- 业务请求发送后不能自动重试；
- 插件不要建立独立权限模型；
- 不直接缓存跨用户凭据；
- 保留 `content`、`structuredContent`、`isError` 和 `_meta` 等协议语义。

MMG 继续统一管理 Token、用户隔离、目录缓存、租约、空闲回收、测试任务和日志。

## 配置验证

`validate(config)` 应：

- 拒绝缺失的必填字段；
- 给出可操作且不含秘密的错误；
- 不进行有副作用的网络调用；
- 不在验证期间启动长期进程。

JSON 导入与管理界面保存都会调用插件验证。
