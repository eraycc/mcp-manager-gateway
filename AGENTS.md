# AGENTS.md

本文件面向使用或维护 MCP Manager Gateway（MMG）的自动化 Agent。目标是让 Agent 以可审计、渐进式、最小权限的方式发现工具、迁移 MCP 配置和修改本仓库。

## 1. 项目心智模型

MMG 是 MCP 网关，不是工具本身。

- 客户端只连接 MMG；
- MMG 保存下游 MCP 配置、权限和能力目录；
- 搜索目录不会启动 `lazy` 服务；
- `gateway_call` 才会按需启动并执行目标工具；
- Agent 可以提交 MCP 提议，但不能绕过人类审批直接创建服务；
- 服务批准后会进入网关目录，客户端通常不需要重启。

除非当前会话确实提供 `gateway_*` 工具，否则不要假设已经连接到 MMG。

## 2. 标准工具调用流程

严格遵循“搜索服务 → 搜索工具 → 精确调用”：

1. 调用 `gateway_search_mcps` 找到服务。
2. 调用 `gateway_search_tools` 获取完整 `inputSchema` 和精确 `gateway_name`。
3. 按 Schema 构造参数。
4. 调用 `gateway_call`，`name` 必须使用搜索结果中的精确名称。
5. 对分页结果保持原查询不变并跟随 `next_cursor`。

规则：

- 不猜测服务 slug、工具名、参数名或枚举值；
- 不把自然语言描述当作 `gateway_name`；
- 不因为列表中已有缓存就假设下游进程正在运行；
- 发现失败时报告 `status`、失败范围和可操作原因；
- 参数校验失败时依据字段路径修正，不用模糊匹配绕过；
- `outcome_unknown` 表示副作用可能已发生，禁止自动重试。

资源工作流：

1. 使用 `gateway_list_resources` 查找资源、提示词或模板；
2. 只使用返回的精确 URI 调用 `gateway_read_resource`；
3. 读取资源是只读操作，不把它当成业务工具执行入口。

## 3. MCP 配置迁移

推荐让 Agent 迁移，而不是让用户逐条手工录入。

可读取的来源包括：

- 通用 `mcpServers` JSON；
- Codex `config.toml`；
- Claude 配置；
- DSH Cordis/registry；
- 用户明确指定的其他结构化配置。

迁移前：

1. 识别来源格式；
2. 枚举所有候选 MCP；
3. 保留命令、参数、URL、transport 和必要环境变量名；
4. 标记密钥、Token、密码、私有路径和高权限命令；
5. 去重，但不要擅自合并行为不同的配置；
6. 先提交提议，不修改来源文件。

### 单条提议

调用 `gateway_mcp_proposals` 时可使用：

~~~json
{
  "name": "Example MCP",
  "slug": "example-mcp",
  "description": "简明说明它解决什么问题",
  "tags": ["development"],
  "transport": "stdio",
  "config": {
    "command": "example-command",
    "args": ["--flag"]
  },
  "source": "codex-config",
  "purpose": "迁移现有 MCP 配置",
  "declared_capabilities": ["描述可用能力"],
  "requested_permissions": ["描述文件、网络或账户权限"]
}
~~~

### 批量提议

优先批量提交，单批 1–100 条：

~~~json
{
  "proposals": [
    {
      "name": "Example MCP",
      "slug": "example-mcp",
      "description": "示例服务",
      "tags": ["development"],
      "transport": "stdio",
      "config": {
        "command": "example-command",
        "args": ["--flag"]
      },
      "source": "codex-config",
      "purpose": "迁移现有配置",
      "declared_capabilities": ["示例能力"],
      "requested_permissions": ["示例权限"]
    }
  ]
}
~~~

约束：

- `transport` 使用网关支持的类型；导入中的 `http` 可规范化为 `streamable-http`；
- `slug` 应稳定、可读、唯一，服务创建后不应修改；
- `config` 只保留运行必需字段；
- 不在提议里加入 `mode`、`isolation` 或 `config_isolation`；这些只能由审批者决定；
- 不伪造测试通过、OAuth 已授权或用户批准；
- 不提交与迁移无关的 MCP。

### 查询审核状态

使用：

~~~json
{
  "action": "list",
  "status": "pending",
  "page": 1,
  "page_size": 50
}
~~~

可用状态包括 `pending`、`incomplete`、`approved` 和 `rejected`。分页查询直到确认每个目标提议的最终状态。

## 4. 人类审批边界

Agent 提议不是安装成功。

提交后应提示用户在 Web 控制台完成：

1. 检查命令、工作目录、URL 和环境变量；
2. 安全地补齐敏感配置；
3. 设置 `lazy` / `eager` / `disabled`；
4. 设置服务、用户或会话实例隔离；
5. 设置 OAuth 配置隔离；
6. 运行真实连接和工具测试；
7. 批准、退回修改或拒绝。

Agent 不得声称 `pending` 或 `incomplete` 的服务已经可用。`approved` 后仍应通过 `gateway_search_mcps`、`gateway_search_tools` 和一次安全的代表性调用做验收。

## 5. 清理旧配置的硬规则

删除客户端原有 MCP 配置是破坏性操作。只有同时满足以下条件才允许执行：

- 用户明确要求或明确批准清理；
- 对应提议全部为 `approved`；
- 每个目标 MCP 已在 MMG 中可发现；
- 至少完成一次适当的安全验收；
- 已保存可恢复备份；
- 能精确定位旧配置中的目标条目。

清理时：

- 只删除已经成功迁移的 MCP 条目；
- 保留无关设置、注释和格式；
- 保留 MMG 自身连接；
- 不删除原配置文件；
- 不删除密钥文件、数据库或 MMG 数据目录；
- 结果有歧义时停止并请用户确认。

清理后重新读取配置并报告保留项、删除项和备份位置。

## 6. 凭据与隐私

- 不在聊天、提议、日志或提交记录中回显真实 Token、密码、OAuth secret；
- 用环境变量名或占位符表达敏感依赖；
- 不擅自把本地凭据上传到网关；
- OAuth 未授权或过期通常是用户级问题，不应改变其他用户的服务策略；
- embedding 检索只应发送描述性目录信息，不发送连接凭据或业务调用参数；
- 管理员 Token 仅用于必要的管理操作，迁移结束后建议撤销提议权限或轮换 Token。

## 7. 状态与失败处理

`mode` 和 `status` 是不同维度：

- `mode`：`lazy`、`eager`、`disabled`；
- `status`：`stopped`、`ready`、`running`、`failed`。

失败处理原则：

- 报告原始错误、服务、作用域和最后一次已知状态；
- 不用空结果冒充成功；
- 不自动启用管理员手动禁用的服务；
- 不自动重放有副作用或结果未知的调用；
- 发现缓存可用但刷新失败时，说明返回的可能是上一版能力；
- 修复配置后重新发现并验证，再报告恢复。

## 8. 修改本仓库

### 运行环境

- Python 由 `uv` 管理；
- 运行 Python、测试或项目命令时使用 `uv run ...`；
- 不直接调用系统 `python`；
- 只修改任务范围内的文件，不覆盖用户已有改动。

常用命令：

~~~console
uv sync --frozen --group dev
uv run mmg --version
uv run pytest tests -q
node --test tests/frontend/core.test.mjs
uv build
~~~

### 代码边界

- 后端代码：`src/mcp_manager/`
- 前端：`src/mcp_manager/static/`
- 数据库迁移：`src/mcp_manager/migrations/`
- 测试：`tests/`
- 公开文档：`wiki/`
- 用户入口文档：英文 `README.md`、中文 `README.zh-CN.md`

### 公开文档规则

- 对外文档只写稳定功能和通用示例；
- 不链接未公开的开发过程目录；
- 不包含个人用户名、真实本机路径、真实 Token、密钥或内部环境描述；
- 路径、域名和凭据统一使用明显占位符；
- 用户可见行为变化时同步更新 `README.md`、`README.zh-CN.md` 与 `wiki/`，并保持三者互链。

### 实现原则

- 保持授权、缓存和运行实例按正确用户/服务/会话作用域隔离；
- 凭据与授权失败应 fail closed；
- 搜索和目录操作不得意外启动 `lazy` 下游；
- 不破坏精确 `gateway_name`、完整 `inputSchema` 和稳定分页契约；
- 新增传输实现应遵守 `mcp_manager.transports` entry point 约定；
- 数据库变更必须通过迁移脚本，不在启动路径临时修改表结构；
- Windows 与 POSIX 行为都要考虑，POSIX 语义不能只用 `sys.platform == "linux"` 判断。

### 验证与报告

按风险选择最小但真实的验证：

1. 先运行直接覆盖改动的测试；
2. 再运行相邻模块测试；
3. 文档变更检查链接、命令、包构建和 Markdown；
4. 不把定向测试通过描述成全量回归通过；
5. 报告运行了什么、结果如何，以及未运行什么。

提交前至少检查：

~~~console
git diff --check
git status --short
~~~

本仓库的用户价值优先级是：减少 Agent 启动和上下文成本、实现渐进式发现与懒启动、让一次配置可被多个 Agent 长期复用，同时保留人类对权限、隔离、测试和批准的最终控制权。
