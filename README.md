# MCP Manager Gateway

> 让 MCP 像 Skills 一样按需发现、按需启动：Agent 只看见一个稳定入口，需要时再找到并调用真正的工具。

[![PyPI](https://img.shields.io/pypi/v/mcp-manager-gateway)](https://pypi.org/project/mcp-manager-gateway/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-manager-gateway)](https://pypi.org/project/mcp-manager-gateway/)
[![License](https://img.shields.io/github/license/eraycc/mcp-manager-gateway)](LICENSE)

MCP 很有用，但 MCP 越配越多以后，问题也会一起放大：

- 每个 Agent 启动时都要连接大量 MCP，启动越来越慢；
- 全量工具定义被提前塞进上下文，真正开始工作前就消耗大量 token；
- 很多 MCP 平时根本用不到，却仍然常驻、占用进程和连接；
- 每换一个 Agent 都要重新复制、修改和维护配置；
- Agent 能读懂现有配置，却无法一次性提交给人审核，只能由人逐项手工录入。

MCP Manager Gateway（简称 **MMG**）把这些 MCP 收到一个网关后面。Agent 默认只需要几个发现与调用工具；搜索目录不会启动下游服务，只有真正调用时才会懒启动对应 MCP。管理员可以在 Web 控制台统一配置、授权、测试、审计和管理生命周期。

~~~
Agent / IDE
    │ 只配置一次 MMG
    ▼
MCP Manager Gateway
    ├── 搜索 MCP 与工具（不启动下游）
    ├── 精确调用（需要时才启动）
    ├── Token / 用户 / 权限 / OAuth
    └── 提议 → 人工审核 → 测试 → 批准
            │
            ├── Filesystem MCP
            ├── Database MCP
            ├── Browser MCP
            └── 其他 stdio / HTTP / SSE / REST 服务
~~~

## 核心价值

### 更快、更轻的 Agent 启动

按需发现模式默认只向 Agent 暴露 3 个核心网关工具，而不是一次注入所有下游 MCP 的全部 Schema。Agent 先搜索，再读取目标工具的完整参数定义，最后精确调用。

这是一种和 Skills 相似的渐进式披露：先知道“有哪些能力”，需要时再展开细节。

### 真正的懒加载与懒启动

查询 MCP、搜索工具和翻页都不会启动下游服务。`lazy` 服务只在第一次业务调用时启动，并在安全的空闲周期后回收；常用服务也可以设为 `eager` 预热，不需要的服务可以设为 `disabled`。

### 一次接入，多 Agent 长期复用

每个 Agent 只连接 MMG，不再分别维护几十份 MCP 配置。服务、凭据、权限和运行状态由网关集中管理；同一套 MCP 可以安全地分配给不同用户和 Token。

### Agent 提议，人类审批

迁移 MCP 时，不建议在管理台里逐条重建。给受信任的管理员 Token 开启“MCP 提议”权限后，Agent 可以：

1. 读取 Codex、Claude、通用 JSON 或 DSH/Cordis 等现有配置；
2. 将多个 MCP 标准化后批量提交到 MMG（单批最多 100 条）；
3. 查询每条提议的审核状态；
4. 由用户在 Web 控制台检查配置、补充隔离策略、真实测试并批准；
5. 批准后立即进入网关目录，已连接的 Agent 无需重启；
6. 全部验证通过并得到用户明确同意后，再删除旧配置中的 MCP 条目，只保留 MMG。

这让迁移从“人工复制配置”变为“Agent 整理并提议，人类掌握最终决定”。Agent 不能在提议阶段自行指定启动策略或隔离级别，也不能绕过审批直接创建服务。

## 5 分钟开始使用

要求 Python 3.12 或更高版本。项目已发布到 [PyPI](https://pypi.org/project/mcp-manager-gateway/)，无需下载本地 wheel。

### 使用 uv 安装（推荐）

~~~console
uv tool install mcp-manager-gateway
mmg
~~~

升级：

~~~console
uv tool upgrade mcp-manager-gateway
~~~

### 使用 pip 安装

建议安装在独立虚拟环境中：

~~~console
pip install --upgrade mcp-manager-gateway
mmg
~~~

安装后 `mmg`、`mcp-manager` 和 `mcp-manager-gateway` 是等价命令。默认启动地址为 <http://127.0.0.1:8765>。首次注册的账户是管理员，密码至少 10 个字符。

首次启动会自动创建用户目录、`.env`、SQLite 数据库和密钥：

- Windows：`C:\Users\<用户名>\.mcp-manager`
- Linux / macOS：`~/.mcp-manager`

需要修改端口时：

~~~console
mmg serve --port 8766
~~~

## 把 Agent 接到 MMG

先在 Web 控制台中创建访问 Token。个人中心会根据当前地址生成可直接复制的 HTTP、stdio、Codex 和通用客户端配置。

统一的 Streamable HTTP 端点是：

~~~text
http://127.0.0.1:8765/mcp
Authorization: Bearer mcpm_你的Token
~~~

不支持远程 HTTP MCP 的客户端可使用 stdio 桥接：

~~~json
{
  "mcpServers": {
    "mcp-manager": {
      "command": "mmg",
      "args": ["stdio", "--url", "http://127.0.0.1:8765"],
      "env": {
        "MCP_MANAGER_TOKEN": "mcpm_你的Token"
      }
    }
  }
}
~~~

桥接进程只负责连接网关；所有下游 MCP 仍由 MMG 统一运行和回收。

## 推荐迁移方式：让 Agent 批量提议

### 1. 开启受控的提议入口

在 Web 控制台创建管理员 Token，并仅为迁移用途开启“MCP 提议”权限。普通用户 Token 看不到提议工具。

### 2. 给 Agent 明确任务

可以直接告诉 Agent：

> 读取我当前客户端中的 MCP 配置，转换为 MMG 提议并批量提交。不要修改原配置；等待我在控制台审核、测试和批准后，再检查迁移状态。只有在我明确同意后，才能删除旧 MCP 配置，只保留 MMG。

更完整的 Agent 操作约束见 [AGENTS.md](AGENTS.md)。

### 3. Agent 提交标准提议

Agent 使用 `gateway_mcp_proposals` 提交单条或批量配置。典型批量参数：

~~~json
{
  "proposals": [
    {
      "name": "Example Filesystem",
      "slug": "example-filesystem",
      "description": "访问指定项目目录",
      "tags": ["filesystem", "development"],
      "transport": "stdio",
      "config": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
      },
      "source": "codex-config",
      "purpose": "迁移现有开发工具",
      "declared_capabilities": ["读取和管理项目文件"],
      "requested_permissions": ["访问 /workspace"]
    }
  ]
}
~~~

提议不会直接创建或启动 MCP。`mode`、`isolation` 和 `config_isolation` 是审批阶段由人决定的字段，Agent 不应把它们塞进提议。

### 4. 人类审核与真实测试

打开“MCP 审批”页面，逐项检查命令、地址、环境变量、目录权限和凭据；根据风险设置：

- 运行策略：`lazy` / `eager` / `disabled`；
- 实例隔离：服务、用户或会话级；
- OAuth 配置隔离；
- 测试参数与预期结果。

测试通过后批准。批准时 MMG 会再次校验相同配置，再将服务加入可用目录。

### 5. 验证后再精简旧配置

让 Agent 查询提议状态，并通过 MMG 搜索、调用已批准工具。只有当所有目标 MCP 均已批准且验证成功，并且用户明确授权清理时，Agent 才能删除旧客户端配置中的对应 MCP 条目。

保留备份，不删除无关配置，不删除 MMG 自身。完成后，每个 Agent 只需保留一个网关入口。

详细导入规则见 [导入工作流](docs/import-workflow.md)。

## Agent 如何渐进式使用工具

按需发现模式的标准顺序是：

| 工具 | 用途 |
| --- | --- |
| `gateway_search_mcps` | 搜索或列出当前 Token 有权访问的 MCP |
| `gateway_search_tools` | 获取目标工具的完整 `inputSchema` 和精确 `gateway_name` |
| `gateway_call` | 使用精确名称和符合 Schema 的参数执行工具 |
| `gateway_list_resources` | 可选：列出资源、提示词和模板 |
| `gateway_read_resource` | 可选：读取精确资源 URI |
| `gateway_mcp_proposals` | 可选：管理员 Token 批量提交或查询 MCP 提议 |

一个可靠的 Agent 不应猜测工具名或参数：先搜索 MCP，再搜索工具，最后使用返回的精确名称调用。搜索和目录分页不会唤醒 `lazy` 服务。

[渐进式发现设计](docs/2026-09-14-progressive-discovery-design.md)解释了搜索、分页、完整 Schema 与精确路由的契约。

## 管理能力概览

- **多种接入方式**：stdio、Streamable HTTP、旧 SSE、REST 转 MCP；
- **统一导入**：通用 JSON、Codex TOML、Claude、DSH Cordis/registry；
- **授权边界**：用户、Token、匿名范围和 MCP 分配相互独立；
- **OAuth 隔离**：个人凭据、能力目录和运行实例按用户隔离；
- **生命周期**：`lazy`、`eager`、`disabled`，支持失败状态与恢复；
- **运行隔离**：共享、用户和会话实例；
- **可观察性**：调用日志、审计日志、后台任务、缓存与失败原因；
- **工具检索**：关键词、拼写模糊匹配，以及可选的 OpenAI 兼容 embedding；
- **安全审批**：Agent 提议、配置编辑、真实测试、批准或拒绝；
- **数据后端**：默认 SQLite，也支持 MySQL。

## 配置与数据

环境变量优先于用户目录中的 `.env`。源码目录和当前工作目录下的 `.env` 不会被自动读取。完整示例见 [.env.example](.env.example)。

常用配置：

~~~dotenv
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=http://127.0.0.1:8765
COOKIE_SECURE=false
DATABASE_URL=
~~~

可通过 `MCP_MANAGER_HOME` 或 `mmg --home /path serve` 指定另一套配置和数据。默认数据包括数据库、`secret.key`、能力缓存、任务状态、调用日志和审计日志。

备份时必须同时保留数据目录、`.env` 和 `secret.key`；密钥丢失后，已有密文凭据无法解密。

## Docker 部署

~~~console
docker compose up --build -d
~~~

默认仅绑定 `127.0.0.1:8765`，数据保存到命名卷 `mcp-manager-data`。远程或生产环境建议使用 HTTPS 反向代理，并设置正确的 `PUBLIC_URL` 与 `COOKIE_SECURE=true`。

MMG 当前按单节点、单网关运行时设计。不要用多个 worker 或多个副本共享同一套下游进程调度。

## 升级与维护

~~~console
uv tool upgrade mcp-manager-gateway
mmg upgrade
~~~

常用维护命令：

~~~console
mmg --version
mmg --help
mmg rebuild-logs
mmg migrate-db --help
mmg migrate-home --help
~~~

## 本地开发

~~~console
uv sync --frozen --group dev
uv run pytest tests -q
node --test tests/frontend/core.test.mjs
uv build
~~~

- 后端：`src/mcp_manager/`
- 前端静态资源：`src/mcp_manager/static/`
- 数据库迁移：`src/mcp_manager/migrations/`
- 测试：`tests/`
- 传输插件说明：[docs/plugins.md](docs/plugins.md)
- 运行时设计：[docs/runtime-design.md](docs/runtime-design.md)
- 验证记录：[docs/validation.md](docs/validation.md)

API 文档在运行中的 `/docs`。

## 安全边界

MCP 配置可以启动本地进程、访问网络和读取数据，应当视为管理员权限。MMG 提供审核、授权、隔离、脱敏和加密存储，但不能把不受信任的 MCP 自动变成安全程序。

- 使用最小权限的运行账户和 Token；
- 提议中不要明文提交真实密钥；
- 对不同信任域使用不同账户或容器；
- 调用有副作用的工具前先核对参数；
- `outcome_unknown` 表示请求可能已经送达，Agent 不应自动重放。

## License

[Apache License 2.0](LICENSE)
