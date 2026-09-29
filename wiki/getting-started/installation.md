# 安装 MCP Manager Gateway

## 环境要求

- Python 3.12 或更高版本；
- 可运行下游 MCP 所需的 Node.js、Python 或其他运行时；
- 默认监听地址 `127.0.0.1:8765`；
- 默认数据库为 SQLite，也可以使用 MySQL。

MMG 已发布到 [PyPI](https://pypi.org/project/mcp-manager-gateway/)。三个入口命令完全等价：

- `mmg`
- `mcp-manager`
- `mcp-manager-gateway`

## 使用 uv 安装

推荐使用独立的 uv tool 环境，避免污染其他 Python 项目：

~~~console
uv tool install mcp-manager-gateway
mmg --version
mmg
~~~

升级安装包：

~~~console
uv tool upgrade mcp-manager-gateway
~~~

## 使用 pip 安装

建议先创建虚拟环境：

~~~console
python -m venv .venv
~~~

激活环境后安装：

~~~console
pip install --upgrade mcp-manager-gateway
mmg --version
mmg
~~~

以后升级：

~~~console
pip install --upgrade mcp-manager-gateway
~~~

`pip` 必须来自安装 MMG 的同一个虚拟环境。若系统中存在多个 Python，请先确认 `mmg` 与 `pip` 指向同一环境。

## 从源码运行

适用于开发和贡献代码：

~~~console
git clone https://github.com/eraycc/mcp-manager-gateway.git
cd mcp-manager-gateway
uv sync --frozen --group dev
uv run mmg
~~~

源码升级由 Git 和 uv 管理：

~~~console
git pull
uv sync --frozen --group dev
~~~

## Docker

~~~console
git clone https://github.com/eraycc/mcp-manager-gateway.git
cd mcp-manager-gateway
docker compose up --build -d
~~~

Compose 默认只向本机发布 `127.0.0.1:8765`，并将数据保存到 `mcp-manager-data` 命名卷。

## 验证安装

~~~console
mmg --version
mmg --help
~~~

启动后打开 <http://127.0.0.1:8765>。第一次注册的账户成为管理员，密码至少 10 个字符。

## 关于 `mmg upgrade`

`mmg upgrade` 不是软件包自更新命令。它只对当前数据目录执行数据库 Schema 迁移，通常不需要在普通升级时手动运行，因为服务启动会初始化数据库。

不同安装方式由不同工具拥有：

| 安装方式 | 升级方式 |
| --- | --- |
| uv tool | `uv tool upgrade mcp-manager-gateway` |
| pip | `pip install --upgrade mcp-manager-gateway` |
| 源码 | `git pull` 后执行 `uv sync` |
| Docker | 拉取新代码或镜像后重新构建/拉取并重建容器 |

MMG 暂不提供自动判断并修改宿主安装环境的自更新命令。这样可以避免错误操作系统 Python、虚拟环境、源码工作区或容器镜像。
