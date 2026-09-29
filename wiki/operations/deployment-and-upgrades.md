# 部署与升级

## 单节点边界

MMG 当前设计为单节点、单网关运行时。不要用多个 worker 或多个副本共享同一套下游进程调度与数据目录。

生产部署应具备：

- 持久数据卷；
- HTTPS 反向代理；
- 正确的 `PUBLIC_URL`；
- 可恢复的数据库与 `secret.key` 备份；
- 运行下游 stdio 服务所需的依赖；
- 最小权限的操作系统账户。

## Docker Compose

~~~console
docker compose up --build -d
~~~

默认端口映射为 `127.0.0.1:8765:8765`，数据卷为 `mcp-manager-data`。容器内 stdio 命令必须安装在容器中；Node.js 等额外依赖应放进派生镜像。

升级源码构建：

~~~console
git pull
docker compose build --pull
docker compose up -d
~~~

如果使用预构建镜像，应先拉取目标版本，再重建容器。不要在运行容器内部用 pip 或 uv 自行修改镜像内容。

## 原生服务

可以用操作系统服务管理器运行 `mmg serve`。只启动一个进程，并配置优雅停止时间，使网关有机会排空调用和关闭子进程。

反向代理应转发：

- `/mcp`；
- `/gateway/v1`；
- `/api`；
- Web 控制台静态资源。

## 软件包升级

### uv tool

~~~console
uv tool upgrade mcp-manager-gateway
mmg --version
~~~

### pip

在安装 MMG 的虚拟环境中：

~~~console
pip install --upgrade mcp-manager-gateway
mmg --version
~~~

### 源码

~~~console
git pull
uv sync --frozen --group dev
uv run mmg --version
~~~

升级前建议备份数据目录和数据库。升级后启动网关，检查健康状态、后台迁移、MCP 目录和代表性工具。

## 数据库迁移命令

~~~console
mmg upgrade
~~~

这个命令只应用数据库 Schema 迁移，不下载或安装新版本。服务启动也会初始化数据库，因此普通包升级通常不需要额外手动运行。

适合手动执行的情况：

- 运维流程要求启动应用前单独迁移；
- 需要在维护窗口中先验证数据库迁移；
- 服务进程已完全停止。

## 为什么不提供通用自更新

同一个 `mmg` 可能来自 uv tool、pip 虚拟环境、源码 checkout、系统包或 Docker 镜像。运行中的程序无法可靠判断哪个工具拥有它，也不应擅自修改宿主 Python 或容器层。

因此包升级由安装工具负责，MMG CLI 只管理应用运行和数据迁移。
