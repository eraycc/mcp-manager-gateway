# 出站网络与代理

[Wiki 首页](../README.md) · [English README](../../README.md) · [中文 README](../../README.zh-CN.md) · [配置与数据目录](configuration.md)

管理员进入 **系统设置 → 网络与代理**。默认关闭代理，服务端 HTTP 请求直连并忽略环境变量代理。启用后选择手动代理或高级“跟随环境变量”模式。配置对 Windows、Linux、macOS 使用相同实现，不读取系统代理设置。

保存只校验字段格式，不要求代理在线。**测试代理连接**使用当前表单进行最长 5 秒的独立测试；结果仅供参考，不会保存配置，也不会阻止后续保存。默认测试 URL 为 `https://example.com/`，可修改为部署环境可达的轻量 HTTP(S) 端点；返回 2xx / 3xx 视为测试通过，测试通过也不保证所有下游地址均可达。

## 手动配置

支持 `http`、`https`、`socks5`、`socks5h`，主机字段只填 hostname 或 IP，端口为 1–65535。协议表示客户端到代理服务器的协议，访问 HTTPS 目标通常仍使用 HTTP 代理。

认证可选无认证或 Basic。Basic 需要用户名，密码允许为空。用户名和密码中的 URL 特殊字符自动编码。标准依赖包含 `httpx[socks]`；当前底层 SOCKS 实现由代理解析目标 DNS，`socks5` 与 `socks5h` 使用相同行为。

名单每行填写 hostname、IP 或 HTTP(S) origin，可带端口，支持 `*`：

- 黑名单：命中名单的目标直连，其他目标走代理；名单为空时全部走代理。
- 白名单：只有命中名单的目标走代理，其他目标直连；名单为空时全部直连。
目标规则按以下优先级检查，黑、白名单使用相同匹配规则：

1. **精确 hostname / IP**：`example.com` 只匹配自身，不自动包含子域名；hostname 不区分大小写，IPv6 支持等价写法。
2. **CIDR**：例如 `192.0.2.0/24`、`2001:db8::/32`，仅匹配目标 URL 中的 IP 字面量；不会解析 hostname 来判断是否属于网段。
3. **`*.` 通配**：`*.example.com` 匹配任意层级的子域名，不包含 `example.com` 自身，也不匹配 `badexample.com`。兼容旧的 `.example.com` 子域名写法；`*` 匹配全部目标。

`https://api.example.com:8443` 或 `https://*.example.com:8443` 可进一步限定协议与端口。每行一项，也支持英文逗号分隔；空行会忽略。与旧版相比，手动名单中的普通 hostname 现在精确匹配；需要同时覆盖主域名和子域名时，请分别添加 `example.com` 和 `*.example.com`。连接测试始终测试代理本身，不受名单限制。

## 自定义 CA 上传

SSL 默认验证证书；选择“自定义 CA”后显示文件上传入口，选择“关闭验证”保留安全警告。无需认证时，用户名、密码及其提示一起隐藏。代理分类只显示自身的保存和连接测试按钮，其他分类继续保留全局保存按钮。

- 只支持一个 PEM CA bundle，最大 1 MiB；多个 CA 请自行合并到同一个 PEM 文件。
- 上传时先用 `ssl.create_default_context(cafile=...)` 加载暂存文件。无效文件提示“不是有效的 PEM CA 文件”，原 CA 不被覆盖。
- 文件固定保存为 `<MCP_MANAGER_HOME>/credentials/proxy-ca.pem`；默认 home 为 `~/.mcp-manager`。重新上传会覆盖当前 CA。
- 页面只显示文件名、上传时间及成功状态，不显示服务器绝对路径；支持“重新上传”和“移除 CA 文件”。
- 上传与移除独立持久化并更新配置版本，不保存其他表单输入。验证方式仍通过“保存代理设置”保存；当前正在使用自定义 CA 时，重新上传的新 CA 应用于后续请求。
- 切回默认验证或关闭验证时，文件保留但不使用；切回自定义 CA 无需重新上传。移除正在使用的 CA 时恢复默认验证。
- 关闭代理后，代理区域中的 SSL 选项不再作用于出站请求；原有服务 TLS 配置继续生效。
- 数据库提交失败时会尝试恢复原文件及时间。备份 home 时应包含 `credentials/`；旧版本保存的服务器路径继续供运行时读取，上传后切换到托管文件，移除操作只删除网关托管文件。

容器部署必须持久化 `MCP_MANAGER_HOME`。默认 Compose 已将该目录设置为 `/data` 并挂载持久卷；自定义容器部署若未挂载，重建后 CA 文件会丢失，需要重新上传。参见[部署与升级](deployment-and-upgrades.md)。

## 环境变量模式

仅在显式启用“跟随环境变量”时使用运行 MMG 进程中的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`（也支持小写名称）。遵循对应协议优先、`ALL_PROXY` 兜底以及 `NO_PROXY` 绕过规则。没有适用代理变量时直连，不回退到操作系统代理设置。手动配置模式忽略这些变量。环境模式的 NO_PROXY 保持兼容：普通 hostname 仍匹配自身及其子域名，不采用手动名单的精确 hostname 规则。

## 生效范围与安全

保存后无需重启；后续新 HTTP 请求读取最新配置，已有响应流继续使用原连接直到结束，包括 SSE 长连接。旧连接排空后释放，当前配置下的请求复用连接池。

统一策略覆盖服务端 REST、Streamable HTTP、SSE、OAuth、嵌入检索及版本检查。服务的“诊断连接”复用实际传输路径。OAuth 的公共地址校验与 DNS 固定保护继续生效：HTTP/HTTPS 代理的 CONNECT 使用已校验 IP，目标 TLS 握手仍使用原始域名；HTTPS 代理自身的证书单独按代理主机验证。发现、授权码换取令牌和刷新令牌均适用。若旧请求失败已将授权标记为失效，修复网络后需重新授权；不会自动重放失败的令牌交换。此设置不控制浏览器翻译、stdio 子进程、外部传输插件或独立客户端到 MMG 的连接。

代理配置加密保存在数据库的 `outbound_proxy` 设置中；保留网关根密钥才能恢复。API 返回的非空密码为 `[REDACTED]`；提交该占位符保留原密码，提交空字符串清除密码。审计只记录模式、版本和修改字段名，不记录密码或完整代理 URL。

管理员 API：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/v1/settings/outbound-proxy` | 读取脱敏配置与版本 |
| PATCH | `/api/v1/settings/outbound-proxy` | 格式校验后保存；可带读取到的 version 防止覆盖并发修改 |
| POST | `/api/v1/settings/outbound-proxy/test` | 测试当前草稿；网络问题返回 ok=false 与参考建议，不修改配置 |
| POST | `/api/v1/settings/outbound-proxy/ca` | 上传单个 CA；JSON 为 `{"pem": "<PEM 文件文本>"}`，文件名由服务器固定 |
| DELETE | `/api/v1/settings/outbound-proxy/ca` | 移除托管 CA，必要时恢复默认验证 |

字段包括 enabled、mode、scheme、host、port、auth_type、username、password、list_mode、list_entries、ssl_verify、validation_url。custom_ca 为只读元数据（filename、uploaded_at、available），不返回或接受服务器绝对路径；上传、移除须使用 CA 专用接口。关闭代理使用 enabled=false；启用时 mode 为 manual 或 env。格式错误返回 422，版本冲突返回 409。
