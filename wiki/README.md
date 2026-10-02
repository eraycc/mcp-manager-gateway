# MCP Manager Gateway Wiki

这里是 MCP Manager Gateway（MMG）的公开文档中心。先阅读 [English README](../README.md) 或[简体中文 README](../README.zh-CN.md)了解项目价值并完成首次运行；本 Wiki 提供部署、迁移、运行机制和扩展开发的详细说明。两份 README 都会链接回本页，下面的索引继续连接各专题文档。

## 推荐阅读路径

### 第一次使用

1. [安装 MMG](getting-started/installation.md)
2. [完成首次启动](getting-started/quick-start.md)
3. [连接 Agent 或 MCP 客户端](guides/connect-clients.md)
4. [迁移已有 MCP](guides/migrate-mcps.md)

### 管理员

- [服务管理与测试](guides/manage-services.md)
- [配置与数据目录](operations/configuration.md)
- [控制台外观、翻译与版本提醒](guides/interface-translation-and-updates.md)
- [部署与升级](operations/deployment-and-upgrades.md)
- [备份与迁移](operations/backup-and-migration.md)
- [安全模型](security/security-model.md)
- [常见问题](troubleshooting/common-issues.md)

### Agent 与集成开发者

- [渐进式发现](concepts/progressive-discovery.md)
- [运行时与生命周期](concepts/runtime-and-lifecycle.md)
- [网关工具参考](reference/gateway-tools.md)
- [CLI 参考](reference/cli.md)
- [传输插件](development/transport-plugins.md)
- [参与开发](development/contributing.md)

## 文档边界

本目录只包含适合公开发布的稳定功能、通用示例和操作说明。示例中的域名、目录、用户名和凭据均为占位符，不能直接用于生产环境。

若文档和当前安装版本的命令帮助不一致，以本机 `mmg --help`、`mmg <command> --help` 和 Web 控制台生成的客户端配置为准。
