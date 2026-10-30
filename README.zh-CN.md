# MCP Manager Gateway

> 让 MCP 像 Skills 一样按需发现、按需启动：Agent 只看见一个稳定入口，需要时再找到并调用真正的工具。

[![PyPI version](https://img.shields.io/pypi/v/mcp-manager-gateway?label=PyPI)](https://pypi.org/project/mcp-manager-gateway/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-manager-gateway?label=Python)](https://pypi.org/project/mcp-manager-gateway/)
[![License](https://img.shields.io/github/license/eraycc/mcp-manager-gateway?label=License)](https://github.com/eraycc/mcp-manager-gateway/blob/master/LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/eraycc/mcp-manager-gateway?label=Release)](https://github.com/eraycc/mcp-manager-gateway/releases)
[![Docker Hub](https://img.shields.io/docker/v/eraycc/mcp-manager-gateway?label=Docker%20Hub)](https://hub.docker.com/r/eraycc/mcp-manager-gateway)
[![GHCR](https://img.shields.io/badge/GHCR-eraycc%2Fmcp--manager--gateway-blue)](https://ghcr.io/eraycc/mcp-manager-gateway)

[English](https://github.com/eraycc/mcp-manager-gateway/blob/master/README.md) | 简体中文 | [完整公开文档与运维指南](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/README.md)

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

大目录的 Web 集成请使用[能力摘要分页与精确详情](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/catalog-api.md)。能力卡片中的长描述、URL 和标识符会在卡片边界内自动换行，适配桌面与手机。定时刷新会重试已手动停止或启动失败的按需、常驻服务，并发布分页目录。停止仅强制关闭当前运行实例，不禁用服务，也不阻止后续调用；后续调用会重新启动按需或常驻服务，常驻服务启动后继续运行。调用与手动、定时能力刷新共用同一目录归属的连续失败计数，默认阈值为 3。未达阈值时保留上次成功的四类能力目录及缓存时间，并展示失败原因；达到阈值才置空目录并标记 failed。任一次调用或完整目录刷新成功即清零，建连成功和 Token 续期本身不清零；内部重试只按最终结果计一次，业务调用不重放。授权失效仍会阻止执行，保留的目录元数据仅对有权访问该服务的原归属用户可见，不改变运行策略；目录查询保持只读，显式精确调用可先重新发现当前定义，再校验和执行。审批通过时清理测试目录条目，已通过、已拒绝记录均不再显示测试能力目录按钮。审批测试支持有界摘要响应；旧全量列表接口有明确的大小限制。Agent 服务摘要提供有界工具名称预览，完整目录通过 `gateway_search_tools` 分页遍历；精确调用走索引路由并复用 Schema 校验器。

服务、审批测试和个人资料的能力卡片显示上游工具注解标题，以及绿色「只读」、红色「破坏性」、蓝色「幂等」、橙色「开放世界」标签；仅布尔值 `true` 显示标签，缺失或无效字段忽略。这些是上游声明，非网关保证。已有服务目录需刷新，审批快照需重新测试以补齐注解摘要。网关六个发现工具均显式声明四项布尔注解；`gateway_call` 保守声明可能破坏、非幂等及开放世界行为。详见[工具注解展示](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/catalog-api.md#工具注解展示)。

能力元数据摘要保持 8 KiB 上限；超大的服务图标、文本、扩展字段和错误列表会生成带有明确省略/裁剪标记的摘要，避免展示元数据阻塞目录发布。完整工具定义和 inputSchema 仍完整保存。

### 观察与释放活跃连接

**连接管理**列出当前用户的业务租期及其引用的多个 MCP 实例；管理员也可以管理他人租期。支持筛选、安全 JSON 详情、确认后单条/批量断开及每 5 秒刷新，隐藏标签页暂停刷新。每条租期区分当前执行、等待派发、累计下游调用与最近派发时间，快速调用完成后仍能看到累计记录；实例列 1～2 项自然增高，3 项及以上采用紧凑封顶高度，超过 3 项可内部滚动。断开释放许可与关联会话：无引用的 lazy 实例排空后停止，eager 实例保留，包括按会话隔离的实例。客户端仍可重新连接，断开不会撤销凭据。系统设置独立管理实例空闲回收和业务租期有效期（默认 43200 秒，0 关闭到期释放）。Bridge 指将 MMG 以 stdio 模式接入客户端的桥接程序，默认每 90 秒心跳，连续缺失 3 次即断开；Bridge 心跳间隔必须为大于 0 的整数（1～86400 秒），失联判定次数设为 0 可禁用失联检测；仍按配置间隔发送心跳、续期并同步配置，连接有效期独立生效。参见[连接管理指南](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/manage-connections.md)。

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

按 Ctrl+C 关闭服务。HTTP 连接和请求任务最多等待 10 秒后取消，随后执行应用资源清理。

建议使用链接了 SQLite WAL-reset 修复版本的 Python：3.51.3+，或已修复的 3.50.7/3.44.6 分支。旧引擎仍可正常启动，仅提示建议升级。使用 `uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"` 检查；仅升级 uv 不会替换已有 Python 环境。详见[运行环境要求](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/storage-governance.md#运行环境)。

首次启动会自动创建用户目录、`.env`、SQLite 数据库和密钥：

- Windows：`%USERPROFILE%\.mcp-manager`
- Linux / macOS：`~/.mcp-manager`

需要修改端口时：

~~~console
mmg serve --port 8766
~~~

### 使用 Docker 部署（从镜像仓库拉取）

两个镜像仓库发布同一套多架构镜像（`linux/amd64`、`linux/arm64`）：

- **Docker Hub** — `eraycc/mcp-manager-gateway` — <https://hub.docker.com/r/eraycc/mcp-manager-gateway>
- **GitHub Container Registry (GHCR)** — `ghcr.io/eraycc/mcp-manager-gateway` — <https://ghcr.io/eraycc/mcp-manager-gateway>

从 Docker Hub 拉取并运行：

~~~console
docker pull eraycc/mcp-manager-gateway:latest
docker run -d --name mmg \
  -p 127.0.0.1:8765:8765 \
  -v mmg-data:/data \
  eraycc/mcp-manager-gateway
~~~

从 GHCR 拉取并运行：

~~~console
docker pull ghcr.io/eraycc/mcp-manager-gateway:latest
docker run -d --name mmg \
  -p 127.0.0.1:8765:8765 \
  -v mmg-data:/data \
  ghcr.io/eraycc/mcp-manager-gateway
~~~

生产环境建议固定到具体版本：

~~~console
docker run -d --name mmg \
  -p 127.0.0.1:8765:8765 \
  -v mmg-data:/data \
  eraycc/mcp-manager-gateway:1.3.9
~~~

数据保存在容器内 `/data` 目录；请挂载到命名卷或宿主机目录。Compose 文件与生产环境说明见 [部署与升级](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/deployment-and-upgrades.md)。

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

更完整的 Agent 操作约束见 [AGENTS.md](https://github.com/eraycc/mcp-manager-gateway/blob/master/AGENTS.md)。

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

详细导入规则见 [迁移已有 MCP](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/migrate-mcps.md)。

## Agent 如何渐进式使用工具

按需发现模式的标准顺序是：

| 工具 | 用途 |
| --- | --- |
| `gateway_search_mcps` | 搜索或列出当前 Token 有权访问的 MCP |
| `gateway_search_tools` | 获取目标工具的完整 `inputSchema` 和精确 `gateway_name` |
| `gateway_call` | 使用精确名称和符合 Schema 的参数执行工具 |
| `gateway_search_resources` | 可选：按服务、类型与匹配方式渐进检索资源、提示词和模板元数据，支持语义检索与稳定分页；`keyword` 为空或 `*` 时浏览全部授权条目，默认每页 20 条，最多 100 条 |
| `gateway_read_resource` | 可选：读取精确资源或提示词 URI；提示词支持字符串类型的 `arguments` |
| `gateway_mcp_proposals` | 可选：管理员 Token 批量提交或查询 MCP 提议 |

资源搜索与读取仍由创建/编辑 Token 时的“启用资源工具”勾选项同时控制，已启用的 Token 无需重建。升级后客户端重启或重连，刷新工具目录后使用新名称。网关工具使用简短参数说明，并分别在 `inputSchema.examples` 中提供一条最小调用示例；OAuth 提议说明包含完整的 `config.auth.auth_mode` 四态取值。

一个可靠的 Agent 不应猜测工具名或参数：先搜索 MCP，再搜索工具，最后使用返回的精确名称调用。搜索和目录分页不会唤醒 `lazy` 服务。

[渐进式发现](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/concepts/progressive-discovery.md)解释了搜索、分页、完整 Schema 与精确路由的契约。

工具响应中的内嵌资源（包括 GitHub MCP 文件正文）保持原样，并以有容量限制的会话快照供原生资源协议及可选资源工具读取。资源检索支持 `kind`、`match`、原始 `uri_prefix` 筛选，静态资源、提示词和模板接入后台向量索引；“系统设置 → 工具检索 → 资源兼容”的 `embedded_resource.inject` 默认关闭，客户端不支持 embedded resource 或上游未提供等价可读资源模板时开启。关闭后停止捕获并清空全部临时快照，重新开启需再次调用工具。临时资源优先显示且最新在前，支持来源 URI 子串与分词搜索；已认证无状态请求复用现有隐式 retention 租约，需要独立快照的 Agent 应使用独立 Token 或显式租约。HTTP 转发根据已授权的缓存 Schema 镜像 `x-mcp-header` 参数。调用日志保留原始上游 `result`，只将临时 URI 提示等新增加工内容补记到 `gateway_processing.added_content`。

同组的 `exposeNativeResources`（“对外暴露原生资源”）默认开启；关闭仅让原生 `resources/list`、`prompts/list`、`resources/templates/list` 返回空，包含临时快照索引。已知标识的原生 `resources/read`、`prompts/get` 仍按既有权限可用。令牌的“启用资源工具”独立控制 `gateway_search_resources`、`gateway_read_resource`，两个开关互不联动。容量、隔离与关键词降级规则见[内嵌资源兼容与检索](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/embedded-resources.md)。

## 管理能力概览

控制台支持确认后导出全部服务的脱敏 JSON 配置；MCP 服务、审批、访问令牌、调用日志和用户列表支持跨页勾选与批量操作。服务列表的“能力列表”和个人资料的“可访问的服务”显示缓存中的工具、资源、提示词及资源模板数量，个人资料同时显示服务描述。管理员和普通用户均可在个人资料中配置、修改按用户隔离服务的 Bearer、Basic、API Key 和个人独享 OAuth 凭据，保存后仍保留编辑入口。已完成认证的 HTTP 类用户隔离服务支持在凭据或 OAuth 授权弹窗中手动刷新本人的能力缓存。管理员服务列表显示当前管理员的能力或未认证状态，以及有访问权的启用用户中的已认证人数。详见[服务管理与测试](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/manage-services.md)。

- **多种接入方式**：stdio、Streamable HTTP、旧 SSE、REST 转 MCP；
- **自动目录刷新**：新增、导入、审批通过与复制服务后自动尝试刷新分页能力目录，成功后直接显示；
- **统一导入导出**：通用 JSON、Codex TOML、Claude、DSH Cordis/registry；导出采用通用 `mcpServers` 连接字段，MMG 专用设置保存在版本化的 `config` 扩展中，支持完整重新导入（[格式说明](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/catalog-api.md#service-configuration-export)）；
- **授权边界**：用户、Token、匿名范围和 MCP 分配相互独立；
- **OAuth 隔离**：个人凭据、能力目录和运行实例按用户隔离；
- **生命周期**：`lazy`、`eager`、`disabled`，支持失败状态与恢复；
- **运行隔离**：共享、用户和会话实例；
- **可观察性**：调用日志、审计日志、后台任务、缓存与失败原因；定时能力目录刷新逐项记录服务、目录归属及实际结果，实例自动停止与租期超时释放通过租期及操作 ID 关联，复用现有审计动作；管理员可筛选查看审计记录、导出 JSON/CSV、确认后后台清理和重建索引。审计与调用日志分别设置保留天数（各自默认 0 为无限），支持清理留痕与中断恢复（[审计日志管理](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/audit-logs.md)）；
- **控制台体验**：浅色、深色或跟随系统，统一的账户菜单和清晰的功能图标；
- **可选网页翻译**：多语言切换、通道选择、私有服务、忽略规则、术语与导入导出；
- **版本提醒**：“系统设置 → 关于”的“自动检测间隔（秒）”控制上次获取的 PyPI 最新版本信息的缓存有效时长，默认 86400 秒（24 小时）。距上次成功检查超过此间隔后，下次登录、打开关于或刷新时联网，否则读取缓存；检查结果自动同步关于页与两处提醒气泡，无需整页刷新；手动“检查更新”始终强制联网。关闭自动检测时仍可修改并保存间隔；提醒与忽略状态按用户保存，不自动修改安装；
- **工具检索**：关键词、拼写模糊匹配，以及可选的 OpenAI 兼容 embedding；
- **安全审批**：Agent 提议、配置编辑、真实测试、批准或拒绝；
- **数据后端**：默认 SQLite，也支持 MySQL；
- **存储治理**：主库保存权威控制数据，`indexes/derived.sqlite` 保存派生索引，缓存和持久正文分域 CAS，调用日志按天保存于 `logs/calls/`；容量预算、后台回收和 SQLite 压缩独立治理（[说明](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/storage-governance.md)）。

## 控制台多语言

MMG 支持可选的浏览器端界面翻译：

1. 管理员进入 **系统设置 → 翻译设置**；
2. 开启“启用全局网页翻译”，选择网页原始语言、默认目标语言和翻译服务，然后保存；
3. 页面右上角会出现语言切换入口；当前浏览器尚无本地选择时，下拉列表默认选中系统设置的目标语言；
4. 在右上角选择实际目标语言，当前页面会立即翻译。

右上角选择的目标语言会持久化保存在当前浏览器中，并优先于系统默认值。只要全局翻译仍处于开启状态，后续再次打开或登录 MMG 时就会自动应用上次选择的语言。系统默认目标语言只用于没有本地选择的浏览器，因此不同浏览器可以各自选择。

浏览器翻译可能会把页面文字发送给所配置的翻译服务。包含敏感业务内容的部署应使用经过审核的私有服务，或保持翻译关闭。通道、忽略规则、缓存清理和版本检测说明见[控制台外观、翻译与版本提醒](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/interface-translation-and-updates.md)。

## OAuth 客户端注册

远程 HTTP/SSE OAuth 使用单一四态注册方式：预注册（Client ID 必填、Secret 可选）、优先 CIMD 且失败回退 DCR（新表单默认）、仅 CIMD、仅 DCR。优先模式在 CIMD 前置条件不满足，或经校验的授权响应明确拒绝客户端或元数据时回退 DCR；DCR 仍需服务器提供注册端点。旧配置保留原有选择策略。进入 OAuth 弹窗后点击“开始授权”或“重新授权”才发起流程；仅打开弹窗不会注册客户端；已有 Token 过期且存在刷新凭据时，会先尝试续期。

服务管理和个人资料的 OAuth 弹窗均提供“重置授权”，二次确认会明确保留已有 Token。重置只清除当前 MCP、对应授权作用域内的待完成授权 state 与 DCR 注册缓存；用户隔离时仅影响本人，包括采用共享配置的情况，CIMD 验证、已有 Token 及其刷新客户端凭据均保留。先关闭旧授权标签页，再重置并重新开始授权。每次开始授权会替换同作用域旧 state；state 有效期为 10 分钟且仅可使用一次。个人资料弹窗读取最新共享配置的只读摘要，个人独享时显示本人最新配置。

CIMD 支持两种元数据托管方式。本机／内网 MMG 可选 **外部静态托管**：填写公网 HTTPS JSON URL，生成并发布文件，再验证线上内容。只有这份不含凭据的静态文件需要公网入站访问，MMG 可以保留在内网。实际回调仍为 `PUBLIC_URL + 所选路径`，必须与文档中的一个完整地址逐字符一致，并由同一已登录浏览器访问。HTTP 回环仅适用于浏览器与 MMG 同机；手机／局域网使用浏览器信任证书的 HTTPS 地址。公网 HTTPS 网关可保留 **MMG 托管**，路径为 `/oauth/client-metadata.json`。两者均受授权服务器策略约束，验证通过不等于授权成功。Token 过期时先续期，再固定连接与目录作用域。授权回调、正常调用、手动/定时目录刷新及个人资料中的能力刷新共用目录准备流程；准备、只读发现或发布期间发生凭据冲突时，重读最新凭据并最多同轮重试一次，续期成功后本轮完成。业务执行前重新解析精确工具路由。共享凭据的并发请求合并续期，按用户隔离的凭据分别处理。续期返回新的 Refresh Token 时，与新 Access Token 一起加密保存后再使用；未返回时保留原 Refresh Token。OAuth 状态检查会尝试续期，存在 Refresh Token 时可点击“刷新授权”。续期失败或下游返回 HTTP 401 后需明确重新授权；不重放业务调用。参见 [OAuth 与内网 MMG 配置指南](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/oauth.md)。

四种注册方式均可设置 Token 端点认证方式：自动选择、none、client_secret_basic 或 client_secret_post。自动 DCR 按服务器能力协商，并加密保存注册返回的 Secret，供交换和刷新使用。CIMD 当前使用 none；显式选择 Secret 方式时，优先模式回退 DCR，仅 CIMD 模式提示不兼容。回退最多启动一次新的 state/PKCE 授权流程；用户拒绝、state/issuer 校验失败及结果不明的 Token 请求不会触发回退。若服务商停留在错误页且未回调 MMG，请切换“仅 DCR”重试。Figma 已知注册响应的限定兼容行为见上述 OAuth 指南。OAuth 表单支持选择回调路径：默认 `/api/v1/oauth/callback`，Figma 等服务可选 `/callback`。预注册和自动模式均使用 `PUBLIC_URL` 拼接所选路径；更改后会清除相关旧注册与 Token，并要求重新授权，不再按服务 URL 自动选择回调路径。DCR 失败会显示已知服务端错误码，不回显响应正文或密钥。

## 配置与数据

环境变量优先于用户目录中的 `.env`。源码目录和当前工作目录下的 `.env` 不会被自动读取。完整示例见 [.env.example](https://github.com/eraycc/mcp-manager-gateway/blob/master/.env.example)。

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

MMG 当前按单节点、单网关运行时设计。不要用多个 worker 或多个副本共享同一套下游进程调度。SQLite、MySQL 8.4 Compose、数据库互转与完整备份说明见 [SQLite 与 MySQL 部署](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/database-deployment.md)。

## 升级与维护

软件包升级由安装方式决定：

~~~console
# uv tool 安装
uv tool upgrade mcp-manager-gateway

# pip 虚拟环境安装
pip install --upgrade mcp-manager-gateway
~~~

源码和 Docker 用户应分别使用 Git + uv 或重新构建/拉取镜像。`mmg upgrade` 只执行数据库 Schema 迁移，不会更新软件包；服务启动通常会自动完成数据库初始化。

完整升级矩阵见 [部署与升级](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/deployment-and-upgrades.md)。

后台清理器按工作预算分批回收过期查询缓存、旧索引和无引用对象。查询缓存支持容量与 TTL；当前目录向量仍按授权作用域、目录版本和模型管理。未变化工具复用同作用域的既有向量，不重复请求模型；禁用服务和发现失败作用域保持不动。旧 `jobs/jobs.jsonl` 不再读取或导入，也不会自动删除。详情见 [缓存与历史残留](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/backup-and-migration.md#缓存与历史残留)。

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

日常 `uv run pytest -q` 默认以最多 4 个进程运行功能回归，包含浏览器和安全边界测试；长耗时性能测试及真实外部服务测试需显式选择。可用 `--suite quick`、`--suite backend`、`--suite browser`、`--domain oauth` 或 `--changed-from <GIT_REF>` 缩小验证范围，`-n 0` 强制串行。`--suite performance` 显式运行压测，默认串行；`--suite all` 包含性能测试，真实外部服务仍需 `--run-live`。每次运行输出初始化、执行和清理耗时。详见[测试分组与保留覆盖的优化](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/development/testing.md)。

- 后端：`src/mcp_manager/`
- 前端静态资源：`src/mcp_manager/static/`
- 数据库迁移：`src/mcp_manager/migrations/`
- 测试：`tests/`
- 传输插件说明：[传输插件](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/development/transport-plugins.md)
- 运行时设计：[运行时与生命周期](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/concepts/runtime-and-lifecycle.md)
- 开发与验证：[参与开发](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/development/contributing.md)
- CLI 参考：[命令行参考](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/cli.md)
- 数据库部署：[SQLite 与 MySQL 部署、迁移和备份](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/database-deployment.md)

完整公开文档见 [Wiki 首页](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/README.md)。运行实例的 OpenAPI 页面位于 `http://<MMG_HOST>:<PORT>/docs`。

## 安全边界

MCP 配置可以启动本地进程、访问网络和读取数据，应当视为管理员权限。MMG 提供审核、授权、隔离、脱敏和加密存储，但不能把不受信任的 MCP 自动变成安全程序。

- 使用最小权限的运行账户和 Token；
- 提议中不要明文提交真实密钥；
- 对不同信任域使用不同账户或容器；
- 调用有副作用的工具前先核对参数；
- `outcome_unknown` 表示请求可能已经送达，Agent 不应自动重放。

## License

Copyright 2026 eraycc. Licensed under the [Apache License 2.0](https://github.com/eraycc/mcp-manager-gateway/blob/master/LICENSE).

大目录可用 `gateway_search_tools` 的 `names_only: true` 渐进获取名称摘要，不读取 Schema；调用前应去掉该参数，按精确名称查询完整定义。跟随 `next_cursor` 时保持原查询不变。原生工具/资源/提示词/模板列表与 `gateway_search_resources` 均分页；整个 MCP JSON-RPC 响应按 1 MiB 预算计入文本与结构化内容的重复占用。单个完整契约超限明确返回 `catalog_entry_too_large`，不截断 Schema。失败服务保留状态和原因，但不展示可执行工具；关键词搜索支持有界的单字符拼写容错，精确调用仍严格匹配名称。
