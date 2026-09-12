# MCP Manager

Python MCP 管理与代理网关，提供独立 Web 管理台、Token 授权、缓存工具目录及按会话复用的懒加载运行时。系统独立保存配置，不修改 Codex 的 config.toml。

## 安装与启动

需要 [uv](https://docs.astral.sh/uv/)。从本地构建好的 wheel 安装（Windows / Linux）：

```console
uv tool install ./dist/mcp_manager_gateway-0.1.3-py3-none-any.whl
mcp-manager
```

安装后提供三个等价命令：`mmg`、`mcp-manager`、`mcp-manager-gateway`，均支持本文的全部子命令和参数。`mcp-manager` 默认启动 Web 服务，也可执行 `mcp-manager serve`。使用 `-h` / `--help` 查看帮助，`-v` / `--version` 查看安装版本。无需进入源码目录。包发布到 PyPI 后，可使用 `uv tool install mcp-manager-gateway`；本仓库的构建操作不会自动发布到 PyPI。

打开 http://127.0.0.1:8765 。首次注册账户为管理员，密码至少 10 个字符。

首次启动自动建立用户目录、.env、SQLite 数据库和密钥，并执行数据库迁移：

- Windows：`C:\Users\用户名\.mcp-manager`
- Linux：`~/.mcp-manager`

工具目录集中保存在 `cache/catalog.jsonl`，后台任务状态集中保存在 `jobs/jobs.jsonl`。缓存文件缺失时，启动后会在后台为已启用服务重建目录，按需服务发现完成即释放实例；仍会禁用发现失败的服务。更新采用追加写入，自动合并重复快照；缓存按服务和授权用户保存最新版本，不再按版本创建文件。启动时会迁移可读取的旧 `.json` 快照，持久化成功后删除旧文件；无法读取的文件会保留并记录迁移错误。日志仍按日期保存为 JSONL，系统数据库、日志 SQLite 索引与 `.env` 保持各自格式。

环境变量优先于用户目录内的 .env；源码目录和当前工作目录的 .env 不会被自动读取。可设置 `MCP_MANAGER_HOME`，或执行 `mcp-manager --home /自定义目录 serve` 指定另一份配置与数据。

修改用户目录下的 .env 后重启服务。更换端口可修改 PORT；使用 OAuth 时还需同步 PUBLIC_URL 中的回调地址。也可执行：

```console
mcp-manager serve --port 8766
```

控制台按浏览器实际访问地址识别同源请求，可使用 localhost、内网 IP 或域名登录，无需把每个地址加入白名单。PUBLIC_URL 用于 OAuth 回调，也作为反向代理场景的额外可信来源。远程访问设置 HOST=0.0.0.0，例如通过 http://192.168.2.111:8765 打开控制台。HTTPS 反向代理场景设置 PUBLIC_URL=https://你的域名、COOKIE_SECURE=true。配置示例见 [.env.example](.env.example)。

## 使用顺序

1. 管理员添加 MCP。支持 stdio、Streamable HTTP、旧 SSE、REST 转 MCP；http 是 Streamable HTTP 的导入别名。
2. 添加或修改启用中的 MCP 后自动发现工具；连接启动时再次更新缓存。lazy 的维护发现完成后释放实例。启动、连接或发现失败（包括 OAuth 未授权、过期）会自动将运行策略设为 disabled，移除可用工具目录并保留失败原因。配置仍会保存，便于修复；修复后管理员可点击启动、刷新自动禁用的服务，或重新设置 lazy/eager，读取最新工具成功才恢复可用目录。自动禁用后的重试恢复此前策略；旧记录未保存此前策略时默认 lazy。手动禁用的服务仅在显式启动或设置启用策略后恢复，刷新不会自行启用。已禁用的 OAuth 服务仍可完成授权，但授权不会自动启用服务。REST 刷新会先探测 HTTP 连接，不能仅凭本地定义生成可用缓存。
3. 给普通用户分配可用 MCP；用户只能在自己的授权范围创建 Token。
4. 在 Token 页面创建凭据，保存只显示一次的完整 Token，然后配置客户端。
5. 实际调用才启动按需服务。查询工具目录不会启动下游进程。

REST 默认向工具地址发送 HEAD 请求检查连接，HTTP 405 表示该路由不支持 HEAD，视为连通；连接失败、鉴权失败、404 或服务器错误会使发现失败。可在表单中填写健康检查 URL（配置字段 `healthcheck_url`），改用 GET 检查固定健康端点并要求 2xx；工具 URL 含参数时必须配置该地址。此检查不执行 POST、PUT、DELETE 等业务工具，也不代替具体工具的功能测试。

管理员可新建、修改、复制、搜索、筛选、分页和批量处理 MCP；支持通用 JSON、Codex TOML、Claude 和 DSH Cordis/registry 导入、诊断、去重、导入冲突策略、脱敏导出、缓存刷新和真实工具测试。批量工具测试运行保存的显式参数用例；先检查参数再执行可能产生写入的工具。

服务标识 slug 是稳定的工具路由名称，创建后不修改；需要新的标识时创建副本。原生工具名通常为 slug__tool，过长或含特殊字符时会生成稳定的短名称。输入 Schema、结构化结果、文本、图像、音频及资源/提示词都通过 MCP 协议返回。

## HTTP 接入

统一端点：POST/GET/DELETE /mcp，使用 MCP Streamable HTTP 客户端。

```http
Authorization: Bearer mcpm_你的Token
```

默认直接列出该 Token 有权使用的全部缓存工具；Token 可切换发现模式，提供 gateway_search、gateway_inspect 和 gateway_call。

关闭 Token 鉴权只影响 MCP 调用，Web 管理仍需登录。匿名范围由管理员单独设置，默认不公开任何服务。请求提供了无效 Token 时，不会退回匿名权限。

系统设置中的跨域来源默认 `*`，作用于 `/mcp` 与 `/gateway/v1`，支持浏览器 Bearer 请求和预检；可改为指定来源列表，保存后立即生效。管理 API 的 Cookie、CSRF 与同源检查保持独立。

匿名客户端如果显式创建 `/gateway/v1/leases` 租约，需要保存响应中的 `client_secret`，并在后续调用、心跳、释放时携带 `X-MCP-Manager-Client`。stdio 桥接会自动处理。

## 本地 stdio 接入

网关服务必须已经运行。桥接进程只连接网关，全部下游 MCP 仍由同一个网关运行时管理。

以下三个命令完全等价，客户端 JSON 的 `command` 也可任选其中一个：

```console
mmg stdio --url http://127.0.0.1:8765
mcp-manager stdio --url http://127.0.0.1:8765
mcp-manager-gateway stdio --url http://127.0.0.1:8765
```

Windows PowerShell：

```powershell
$env:MCP_MANAGER_TOKEN = "mcpm_你的Token"
mcp-manager stdio --url http://127.0.0.1:8765
```

Linux：

```sh
MCP_MANAGER_TOKEN=mcpm_你的Token mcp-manager stdio --url http://127.0.0.1:8765
```

也支持 --token 参数；URL 可通过 MCP_MANAGER_URL 设置。客户端的通用 JSON 配置：

```json
{
  "mcpServers": {
    "mcp-manager": {
      "command": "mcp-manager",
      "args": ["stdio", "--url", "http://127.0.0.1:8765"],
      "env": {"MCP_MANAGER_TOKEN": "mcpm_你的Token"}
    }
  }
}
```

个人中心提供可复制的 HTTP、stdio、Codex 及通用客户端配置示例。

## 生命周期

- lazy：首次工具调用启动；同一共享范围的并发调用只创建一个实例。
- eager：服务共享实例在启动时预热；已有授权的个人 OAuth 按用户预热。其他用户/会话隔离实例需要对应身份首次接入后建立。会话隔离实例在最后引用释放后关闭。
- disabled：不进入可用工具目录，也不接受业务调用。
- 手动停止会设置临时停止状态，直到显式启动或保存新的启用策略。
- 公共服务共享实例；个人 OAuth 按用户隔离；其他有状态服务可选服务、用户、会话隔离。
- stdio 桥接有独立租约，每 30 秒心跳、90 秒失联到期，正常退出主动释放。心跳异常或连接失效后，在下一次新请求前重建连接，已经派发的业务调用不会重放。
- 支持 MCP 会话的 HTTP 客户端可用 DELETE 释放；未报告退出的通用 HTTP 客户端采用保留租约。
- 只有所有引用释放后，按需实例才停止。默认 24 小时没有业务调用也会回收；心跳和目录刷新不算业务调用。活动调用不会被空闲回收杀死。系统设置中的空闲回收时间设为 `0` 时，关闭业务空闲超时回收；主动断开和桥接失联租约仍正常释放。
- 单实例并发、排队、启动、调用与停止超时可配置；停止和配置换代会阻止继续派发排队请求。
- 发送后失去结果标记为 outcome_unknown，不自动重放调用。

Windows 使用 MCP SDK 的 Job Object 清理子进程树；Linux 使用独立进程组。Linux 作为长期服务运行时，建议使用附带的 systemd 单元（KillMode=control-group）或 Docker init，以在网关异常终止时一起回收进程。

## OAuth

在 MCP 认证配置中选择 oauth，填写 authorization_url、token_url、client_id、client_secret（可选）、scopes 和 scope（user/service）。其中 `scopes` 是 OAuth 权限列表（例如 `mcp:read`），`scope` 仅决定网关凭据归属（`user` / `service`），两者互不覆盖。支持授权码、PKCE、一次性 state、Token 刷新及 client_secret_post/client_secret_basic。

回调地址为 PUBLIC_URL/api/v1/oauth/callback。个人用户在个人中心授权自己的服务；共享服务 OAuth 由管理员授权。凭据加密保存，撤销、配置变化和刷新并发会重新验证。

## 数据和迁移

默认配置：

```dotenv
DATABASE_URL=sqlite://mcp-manager.sqlite
# 或
DATABASE_URL=mysql://user:password@host:3306/dbname?charset=utf8mb4
```

MySQL 数据库需要提前创建；系统自动执行表结构升级。SQLite 相对路径以 DATA_DIR 为基准，DATA_DIR 默认是用户配置目录，也支持 Windows/Linux 绝对路径。URL 中密码的特殊字符需要 URL 编码。

目录（可由 MCP_MANAGER_HOME / DATA_DIR 覆盖）：

| 路径 | 用途 |
| --- | --- |
| ~/.mcp-manager/.env | 用户配置 |
| ~/.mcp-manager/mcp-manager.sqlite | 默认系统数据库 |
| ~/.mcp-manager/secret.key | JWT 与配置加密的根密钥 |
| ~/.mcp-manager/cache/ | 按配置版本、身份隔离的能力缓存 |
| ~/.mcp-manager/jobs/ | 可恢复查看的任务状态，不自动重放中断任务 |
| ~/.mcp-manager/logs/YYYY-MM-DD.jsonl | 调用日志原文 |
| ~/.mcp-manager/logs/audit/ | 管理审计 |
| ~/.mcp-manager/logs/deletions/ | 防止删除记录恢复的删除日志 |
| ~/.mcp-manager/indexes/logs.sqlite | 可重建的日志查询与统计索引 |

日志保留天数默认为 `0`（无限保留）。设为 `7` 时，后台自动清理超过 7 天的调用日志原文和查询索引；保存设置后在下一轮维护中检查（通常 5 秒内），之后每小时检查。审计日志独立保留。

个人资料的登录会话显示登录 IP 和设备 UA，支持撤销或彻底删除其他登录会话；旧会话未采集的信息显示为未知。

系统设置的关于页面提供安装版本、[项目主页](https://github.com/eraycc/mcp-manager-gateway)、[发布地址](https://github.com/eraycc/mcp-manager-gateway/release)、[Issue 反馈](https://github.com/eraycc/mcp-manager-gateway/issues)和[作者主页](https://github.com/eraycc)。

备份时保留用户目录内的 .env、整个 DATA_DIR 及系统数据库；SECRET_KEY 或 secret.key 必须保留，否则原凭据无法解密。

旧版源码目录数据可以离线复制到一个空用户目录（原目录保留）：

```console
mcp-manager --home ~/.mcp-manager migrate-home --from-data /旧项目/data
```

旧版本有 .env 时同时传入 `--from-env /旧项目/.env`。命令保留有效密钥，检查 SQLite 完整性；源实例运行中、目标非空或配置文件不存在时拒绝迁移。也可以在完全停止网关后，将整个旧 data 目录直接剪切为新的用户目录，必须保留 secret.key 与日志等配套文件。

停止网关后迁移到一个空目标数据库：

```console
mcp-manager migrate-db --target-database-url "mysql://user:password@host:3306/newdb?charset=utf8mb4"
```

也支持 MySQL → SQLite。迁移保留用户、Token、授权、系统设置、配置和 OAuth 密文；日志与能力缓存继续使用原 DATA_DIR。完成后修改 DATABASE_URL 再启动。目标有业务数据时拒绝覆盖。

仅升级表结构：mcp-manager upgrade。
停止网关后离线重建日志索引：mcp-manager rebuild-logs；运行中可在日志管理执行重建任务。

## Docker / Linux 服务

```console
docker compose up --build -d
```

Compose 使用单个网关进程，数据持久化到命名卷 mcp-manager-data，容器内用户目录为 /data。需要宿主机目录时，将挂载项替换为 /你的持久化目录:/data。生产镜像安装构建出的 wheel，不依赖源码 checkout。容器内配置的 stdio 命令必须安装在容器中；Node 等额外运行时需要在派生镜像中安装。访问其他容器的服务应使用其容器网络地址。

构建环境无法访问 PyPI 时，可通过 `docker build --build-arg UV_DEFAULT_INDEX=https://你的镜像/simple --target production -t mcp-manager .` 指定 Python 包索引；默认仍使用 PyPI。

原生 Linux 服务运行时，网关自身会管理 Windows/Linux 子进程清理（Job Object / 进程组）。每个部署只运行一个网关实例，不使用多 worker 或多副本共享进程调度。

## 开发

后端为 src/mcp_manager 下的独立模块；包内 static/ 为原生 JS ES 模块，通过 /api/v1 与后端通信。迁移脚本在包内 migrations/，测试在 tests/，生成的截图和验收产物在 artifacts/。前端可单独部署，由同源反向代理转发 /api、/gateway 和 /mcp。

```console
uv sync --frozen --group dev
uv run mcp-manager serve
uv run pytest tests -q
node --test tests/frontend/core.test.mjs
uv build
```

浏览器测试需要本机 Chrome 或 Playwright Chromium。MySQL 专项测试通过 MCP_TEST_MYSQL_URL 指向专用的空测试数据库。API 文档在 /docs。

传输插件使用 Python entry point 组 mcp_manager.transports；实现 validate(config) 和异步上下文 connect(spec)，连接对象提供 discover()、call(name, arguments)，可选 read_resource/get_prompt。网关统一处理授权、租约、并发、日志和关闭。插件接口说明见 docs/plugins.md。

本版的部署边界是单节点、单运行时。自定义下游进程可以执行操作系统命令，MCP 配置权限属于管理员；为不同信任域配置不同运行账户或隔离容器。
