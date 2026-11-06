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
- `example.com` 匹配域名自身及其子域名，`.example.com` 仅匹配子域名。
- `https://api.example.com:8443` 仅匹配相同协议与端口。
- 连接测试始终测试代理本身，不受名单限制；实际业务请求按名单路由。

SSL 默认验证证书，也可配置网关服务器上的自定义 CA 文件路径，或临时关闭验证。关闭代理后，代理区域中的 SSL 选项不再作用于出站请求；原有服务 TLS 配置继续生效。CA 文件不存在、证书无效、代理不可达等运行问题由连接测试和实际调用提示，不作为保存前的联网要求。

## 环境变量模式

仅在显式启用“跟随环境变量”时使用运行 MMG 进程中的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`（也支持小写名称）。遵循对应协议优先、`ALL_PROXY` 兜底以及 `NO_PROXY` 绕过规则。没有适用代理变量时直连，不回退到操作系统代理设置。手动配置模式忽略这些变量。

## 生效范围与安全

保存后无需重启；后续新 HTTP 请求读取最新配置，已有响应流继续使用原连接直到结束，包括 SSE 长连接。旧连接排空后释放，当前配置下的请求复用连接池。

统一策略覆盖服务端 REST、Streamable HTTP、SSE、OAuth、嵌入检索及版本检查。服务的“诊断连接”复用实际传输路径。OAuth 的公共地址校验与 DNS 固定保护继续生效。此设置不控制浏览器翻译、stdio 子进程、外部传输插件或独立客户端到 MMG 的连接。

代理配置加密保存在数据库的 `outbound_proxy` 设置中；保留网关根密钥才能恢复。API 返回的非空密码为 `[REDACTED]`；提交该占位符保留原密码，提交空字符串清除密码。审计只记录模式、版本和修改字段名，不记录密码或完整代理 URL。

管理员 API：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/v1/settings/outbound-proxy` | 读取脱敏配置与版本 |
| PATCH | `/api/v1/settings/outbound-proxy` | 格式校验后保存；可带读取到的 version 防止覆盖并发修改 |
| POST | `/api/v1/settings/outbound-proxy/test` | 测试当前草稿；网络问题返回 ok=false 与参考建议，不修改配置 |

字段包括 enabled、mode、scheme、host、port、auth_type、username、password、list_mode、list_entries、ssl_verify、custom_ca_path、validation_url。关闭代理使用 enabled=false；启用时 mode 为 manual 或 env。格式错误返回 422，版本冲突返回 409。
