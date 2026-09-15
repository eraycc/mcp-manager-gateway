# mmg serve Windows 交互式管理脚本

**交互式主菜单**统一管理 `mmg serve`：配置、常驻服务、临时启动。配置存 `.env`（脚本同级目录），通用（`$env:USERPROFILE`，无硬编码用户名），无日志。

> 本文档中 `<repo>` 指 mcp-manager 仓库根目录（本文件所在 `deploy\windows` 的上两级）。
> 所有命令把 `<repo>` 替换为实际仓库路径即可。

## 文件

| 文件 | 作用 |
|---|---|
| `manage.ps1` | 交互式主菜单：配置管理 / 服务管理 / 直接启动（中文 UI，带 UTF-8 BOM） |
| `mmg-daemon.ps1` | 守护脚本：while(true) 拉 `mmg serve`，非 0 退出 10 秒后重启，输出全部丢弃 |
| `.env` | 配置文件（由 manage.ps1 生成/编辑，KEY=VALUE，无 `MCP_MANAGER_` 前缀） |
| `README.md` | 本文档 |

## 启动管理脚本

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<repo>\deploy\windows\manage.ps1"
```

主菜单（中文）：

```
========================================
  mmg serve 管理器
========================================

  [1] 配置与管理
  [2] 服务管理
  [3] 直接启动
  [0] 退出
```

## 1. 配置与管理

子菜单：

```
  [1] 查看当前配置
  [2] 编辑 .env（记事本）
  [0] 返回
```

- **[1] 查看当前配置**：显示 `.env` 当前内容。
- **[2] 编辑 .env（记事本）**：用 notepad 打开 `.env`，保存并关闭后回车重新显示。

`.env` 格式（KEY=VALUE，`#` 注释，无默认值的项默认注释）：

```
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=http://127.0.0.1:8765
# DATA_DIR=./mcp-manager/
# DATABASE_URL=mysql://user:password@localhost:3306/mcp_manager?charset=utf8mb4
# COOKIE_SECURE=false
```

| 键 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8765` | 监听端口 |
| `PUBLIC_URL` | `http://127.0.0.1:8765` | 对外 URL |
| `DATA_DIR` | （无） | 数据目录，默认 home 下 |
| `DATABASE_URL` | （无） | 数据库连接串，默认 SQLite |
| `COOKIE_SECURE` | （无） | cookie 是否强制 HTTPS |

守护脚本启动时读取 `.env`，把非注释的键映射成 `MCP_MANAGER_*` 环境变量（HOST/PORT 走 `--host/--port`，其余走 env），所以**改 `.env` 后重启服务/程序才生效**。

## 2. 服务管理（常驻）

子菜单：

```
  [1] 安装为常驻服务
  [2] 启动服务
  [3] 停止服务
  [4] 重启服务
  [5] 卸载服务
  [6] 查看服务状态
  [0] 返回
```

- **安装**：写 HKCU Run 键（`mmg-gateway`），登录时自动启动守护（无管理员权限要求；HKCU 是当前用户，mmg 需要读 `~/.mcp-manager` 凭据）。安装后可选立即启动。
- **卸载**：停 mmg + 删 HKCU Run 键。
- **状态**：显示 mmg 进程 / 守护进程 / 端口（读 .env 的 PORT）/ autostart 是否安装。

> 「服务」= HKCU Run 自启 + 守护脚本。重启电脑后登录会自动拉起。

### 状态显示示例

```
--- 服务状态（端口 8765）---
  mmg: 运行中 (PID 12345)
  守护: 运行中 (PID 67890)
  端口 8765 : 监听中
  自启 (HKCU Run): 已安装
```

> 端口号动态读 `.env` 的 `PORT`，不写死 8765。改 `.env` 端口后重启服务，状态显示自动跟随。

## 3. 直接启动（临时后台）

子菜单：

```
  [1] 启动程序（临时后台）
  [2] 停止程序
  [3] 查询程序状态
  [0] 返回
```

- **启动**：临时后台拉守护脚本（Hidden 窗口），**重启电脑后不会自动恢复**（区别于「服务」）。
- **停止**：杀守护进程 + mmg 子进程。
- **状态**：显示 mmg 进程 / 端口（读 .env 的 PORT）。

### 状态显示示例

```
--- 直接启动状态（端口 8765）---
  mmg: 运行中 (PID 12345)
  端口 8765 : 监听中
```

## 两种启动方式的区别

| | 服务（服务管理） | 直接启动 |
|---|---|---|
| 持久化 | 是（HKCU Run，登录自启） | 否（临时，重启需重新启） |
| 启动命令 | `manage.ps1` → [2]→[1]/[2] | `manage.ps1` → [3]→[1] |
| 停止命令 | `manage.ps1` → [2]→[3] | `manage.ps1` → [3]→[2] |
| 崩溃自愈 | 是（守护 while 循环） | 是（守护 while 循环） |
| 日志 | 无 | 无 |

## 验证

```powershell
Get-Process -Name mmg -ErrorAction SilentlyContinue | Select-Object Id,StartTime
# 端口用 .env 的 PORT（默认 8765）
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

端口在监听即成功。

## 为什么 .env 放脚本目录而非 ~/.mcp-manager

mcp-manager 的 `Settings`（pydantic-settings）默认读 `MCP_MANAGER_HOME/.env`（即 `~/.mcp-manager/.env`），env 前缀 `MCP_MANAGER_`。
但用户要求配置存**脚本同级目录**（`deploy\windows\.env`），且用无 `MCP_MANAGER_` 前缀的短键名（HOST/PORT/...）。
所以 `mmg-daemon.ps1` 启动时**显式读 `deploy\windows\.env`**，把键映射成 `MCP_MANAGER_*` 环境变量（HOST/PORT 走 CLI 参数），再启动 mmg。
这样配置集中在脚本目录，方便版本管理和单点编辑，不污染 `~/.mcp-manager`。
