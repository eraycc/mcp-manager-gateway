# 参与开发

## 环境

Python 由 uv 管理：

~~~console
uv sync --frozen --group dev
uv run mmg --version
~~~

运行 Python 模块、测试和工具时使用 `uv run`。

## 目录

- `src/mcp_manager/`：后端与运行时；
- `src/mcp_manager/static/`：Web 前端；
- `src/mcp_manager/migrations/`：数据库迁移；
- `tests/`：自动化测试；
- `wiki/`：公开文档；
- `AGENTS.md`：Agent 使用与仓库协作约束。

公开文档只写稳定功能和通用示例，不加入开发现场、个人路径、真实凭据或内部过程记录。

## 常用验证

~~~console
uv run pytest tests -q
node --test tests/frontend/core.test.mjs
uv build
git diff --check
git status --short
~~~

先运行直接覆盖改动的定向测试，再根据风险扩大范围。不要把定向通过表述为全量回归通过。

## 核心契约

修改时必须保持：

- 搜索目录不启动 `lazy` 服务；
- discovery 返回精确 `gateway_name` 和完整 `inputSchema`；
- 执行只按精确名称路由；
- 权限在目录、排队和派发阶段都正确检查；
- OAuth、缓存和实例按正确主体隔离；
- 结果未知的业务调用不自动重放；
- Windows 与 POSIX 平台行为明确；
- 数据库变化通过迁移脚本实现。

## 文档更新

用户可见行为变化时同时更新：

1. 根 `README.md` 的简明说明；
2. `wiki/` 中对应详细文档；
3. `AGENTS.md` 中影响 Agent 行为的硬规则；
4. CLI `--help` 或控制台提示。

所有相对链接必须在仓库中存在，示例凭据必须是明显占位符。
