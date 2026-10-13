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
