# 配置与数据目录

## 配置优先级

进程环境变量优先于 MMG 用户目录中的 `.env`。项目源码目录和当前工作目录中的 `.env` 不会自动读取。

可以使用：

~~~console
mmg --home <MMG_HOME> serve
~~~

或设置 `MCP_MANAGER_HOME` 指定独立实例目录。

## 常用环境变量

~~~dotenv
DATA_DIR=.
DATABASE_URL=
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=http://127.0.0.1:8765
COOKIE_SECURE=false
~~~

| 变量 | 说明 |
| --- | --- |
| `DATA_DIR` | 数据文件目录；相对路径基于 MMG 主目录 |
| `DATABASE_URL` | 空值使用默认 SQLite；也支持 SQLite/MySQL URL |
| `HOST` | 监听地址 |
| `PORT` | 监听端口 |
| `PUBLIC_URL` | 外部访问与 OAuth 回调基准地址 |
| `COOKIE_SECURE` | HTTPS 部署时设为 `true` |
| `MCP_MANAGER_HOME` | 配置与默认数据根目录 |

修改 `.env` 后重启服务。

## 默认目录

默认主目录：

- Windows：`%USERPROFILE%\.mcp-manager`
- Linux / macOS：`~/.mcp-manager`

主要内容：

| 路径 | 用途 |
| --- | --- |
| `.env` | 运行配置 |
| `mcp-manager.sqlite` | 默认系统数据库 |
| `secret.key` | JWT 与配置加密根密钥 |
| `indexes/derived.sqlite` | 目录、词项、向量、日志查询及缓存元数据的单一派生库 |
| `objects/cache/sha256/` | 可重建正文 CAS |
| `objects/durable/sha256/` | 任务结果与审批测试证据 CAS |
| `catalog/manifests/` | 当前及有界历史目录版本指针 |
| `logs/calls/YYYY-MM-DD.jsonl` | 调用日志原文 |
| `logs/audit/YYYY-MM-DD.jsonl` | 管理审计原文 |
| `backups/` | 升级备份与归档；需纳入手动保留管理 |

`secret.key` 必须与数据库一起备份。丢失后已有加密凭据无法恢复。

## 数据库

默认：

~~~dotenv
DATABASE_URL=sqlite://mcp-manager.sqlite
~~~

MySQL 示例：

~~~dotenv
DATABASE_URL=mysql://mcp_user:<PASSWORD>@db.example.com:3306/mcp_manager?charset=utf8mb4
~~~

MySQL 数据库需要预先创建。URL 中的特殊字符必须编码。迁移数据库时先停止网关，并使用空目标数据库。

## 网络设置

本地开发使用 `HOST=127.0.0.1`。远程部署使用反向代理时通常设置：

~~~dotenv
HOST=0.0.0.0
PUBLIC_URL=https://mcp.example.com
COOKIE_SECURE=true
~~~

控制台还可以配置：

- MCP 端点允许的 Host/IP；
- 跨域来源；
- 匿名访问范围；
- 调用日志保留天数与审计日志保留天数（独立设置，各自默认 0 为无限）；
- 失败阈值和空闲回收；
- embedding 检索连接。

这些设置和管理 API 的 Cookie、CSRF、同源校验是不同边界。
