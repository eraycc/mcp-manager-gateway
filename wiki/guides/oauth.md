# OAuth 预注册、CIMD 与 DCR

[文档首页](../README.md) · [English README](../../README.md) · [简体中文 README](../../README.zh-CN.md) · [服务管理](manage-services.md)

MMG 作为 OAuth 客户端访问下游 MCP；这与 Agent 登录 MMG 所使用的访问 Token 相互独立。新增、编辑、审批和个人独享配置使用相同字段规则。

## 选择注册方式

| 方式 | 填写内容 | 使用条件 |
| --- | --- | --- |
| 预注册（默认） | Client ID 必填；Secret 可选；可填写授权、Token 地址与 Scopes | 已在服务提供方创建客户端；端点留空时尝试发现 |
| 自动：CIMD 优先 | 无需手填 Client ID/Secret | 授权服务器声明支持 CIMD，MMG 有稳定的公网 HTTPS 地址 |
| 自动：DCR 回退 | 显式勾选“允许 DCR 回退”；客户端名称必填，默认 Codex | 授权服务器未声明支持 CIMD，并提供 registration_endpoint |

DCR 是可选兼容机制，默认关闭。服务器已声明 CIMD 时，即使 MMG 的公网地址未配置好，也会提示修复 PUBLIC_URL，不降级到 DCR。客户端名称仅影响 DCR 注册请求的显示名称；填写 Codex 不会取得官方 Codex 客户端的身份或特权。

切换模式会清除不适用的 Client ID/Secret 和客户端名称，保留用户选择的 Token 端点认证方式。新增、编辑、审批与个人独享配置均可设置以下选项：

| Token 端点认证方式 | 预注册 | 自动注册 |
| --- | --- | --- |
| 自动选择 | 有 Secret 时按服务器能力选择 Basic/Post，无 Secret 时使用 none | CIMD 使用 none；DCR 按服务器声明依次选择 none、client_secret_post、client_secret_basic |
| none | 不填写 Secret，配合 PKCE | 请求公开客户端注册；不会发送注册响应中多余的 Secret |
| client_secret_basic | 必须填写 Secret，使用 HTTP Basic | DCR 必须返回有效 Secret，使用 HTTP Basic |
| client_secret_post | 必须填写 Secret，将其放入表单请求体 | DCR 必须返回有效 Secret，将其放入表单请求体 |

每次 Token 请求只使用一种认证方式，不在失败后自动改用另一种方式或重复交换授权码。显式选择的方式必须受服务器支持；不支持时在注册前提示。CIMD 的公开文档固定为 none，显式选择 Secret 方式会提示不兼容，不绕过 CIMD 优先规则降级到 DCR。

DCR 返回的 Client ID、Secret、认证方式和可选 Secret 有效期按配置归属加密缓存，不回填到可编辑的服务配置，也不出现在授权 URL、公开元数据或审计日志中。有效注册可复用；更改认证方式使旧注册及待处理授权失效；重新注册取得不同的 Client ID、Secret 或认证方式时，相关配置作用域内的旧授权一并作废，避免用新客户端刷新旧 Token。Secret 缺失、非法或过期时停止并要求重新授权；只有用户明确开始授权时才可重新注册，业务调用不自动注册。

Figma 的限定兼容：当发现的 issuer、资源、注册端点和 Token 端点均为其已知官方地址，且已协商 Secret 认证、注册响应却声明 none 并返回 Secret 时，MMG 按请求的 Secret 认证方式处理。其他服务器的矛盾响应不套用此规则。Figma 场景可以选择 client_secret_post；名称、端点、回调、权限及用户授权仍须由服务器接受，实际联调成功才代表可用。

Figma 的本地回调：使用 HTTP 回环地址（localhost、127.0.0.1 或 ::1）访问 MMG 时，Figma 自动注册改用同源、同端口的 `/callback`，例如 `http://127.0.0.1:8765/callback`。Figma 可能拒绝 `/api/v1/oauth/callback` 并返回 `invalid_redirect_uri`；手动填写授权、Token、注册或 Issuer URL 不会改变这个回调地址。其他 MCP、预注册客户端及公网 HTTPS 的回调路径保持原有规则。

`/callback` 与原回调共用登录用户、会话、state、PKCE、issuer、资源及授权代次校验；使用与 PUBLIC_URL 相同的主机名登录，避免切换 localhost 与 127.0.0.1 导致会话丢失。注册、授权链接、交换授权码和缓存绑定使用同一回调地址，不在失败后悄悄更换地址重试。

注册失败时，提示保留 HTTP 状态并仅显示允许的 OAuth 错误码（如 `invalid_redirect_uri`、`invalid_client_metadata`），不回显任意 error_description、响应正文或密钥。Figma 的服务器准入规则仍适用；DCR 成功只代表客户端已注册，仍需在浏览器完成用户授权。

## 发现、授权与缓存

Scopes 用于指定申请的访问权限，多个值按 OAuth 2.0 标准以空格分隔，例如 `mcp:connect profile:read`；实际值以服务提供方要求为准。`mcp:connect` 是 Figma 等服务的常见值，可自行修改；进入端点自动发现流程时，留空则使用发现的权限信息，不强制给所有 MCP 填入相同值。

先发现受保护资源元数据，再发现所选授权服务器的 OAuth/OIDC 元数据；发现支持 401 challenge、路径及根 well-known 地址。端点使用 HTTPS，本机回环可使用 HTTP；不跟随元数据重定向，不转发 MMG 或下游凭据。公共 MCP 引导的请求会校验 DNS 解析地址，并固定连接到已校验的公共 IP，保留 TLS 主机名验证。单次元数据/注册请求有 8 秒总时限，Token 请求有 30 秒总时限，JSON 响应上限 256 KiB。

1. 保存配置。此时不执行 DCR。
2. 打开“授权”弹窗，查看当前用户状态。
3. 明确点击“开始授权”或“重新授权”，才发现端点、选择 CIMD 或注册 DCR，并生成 PKCE 授权链接。
4. 点击“打开授权页面”，在服务提供方确认权限。
5. 返回后重新检查授权状态并发现能力；禁用的服务不会因此自动启用。

DCR 注册信息加密缓存，按 MCP、配置归属、实际资源地址、issuer、注册端点和回调地址绑定。共享配置可以复用注册客户端，各用户 Token 仍分别保存；个人独享配置单独注册。更改配置、资源地址或回调地址会要求重新授权；issuer 改变会作废相关旧授权。环境变量形式的资源 URL 在授权、回调与使用时按实际展开地址核对。DCR 网络失败可能已在对方创建客户端，MMG 不自动重试，应检查对方记录后再手动发起。

回调绑定原用户、会话、服务版本、授权代次与 10 分钟有效的 state，使用 PKCE S256，并核对所发现 issuer 的 iss 响应要求。授权和 Token 请求携带绑定的 resource。并发回调或旧请求的延迟 401 不能覆盖新授权。

## Token 状态与恢复

- valid：Token 尚未进入过期窗口；未提供 expires_in 时视为有效期未知，直到下游拒绝。
- expired：Token 已过期或将在 60 秒内过期；有 Refresh Token 时，实际使用最多刷新一次。
- needs_reauth：无有效授权、无可用刷新凭据、刷新失败或下游明确返回 HTTP 401。

刷新失败及 401 会持久记录“需要重新授权”；重启、定时发现和再次调用都不会继续自动刷新，更不会自动重新注册客户端或重放业务调用。重新授权成功后清除终态。断开授权清除 Token，保留可复用的注册信息，不代表撤销服务提供方的授权；需要彻底撤销时请同时到提供方操作。

## 公网 HTTPS 与 Nginx

CIMD 的 Client ID 是 MMG 托管的 HTTPS 元数据 URL。授权服务器需要从公网读取它；不要求把管理页面开放给所有人。浏览器也必须能够访问同一 PUBLIC_URL 的控制台与回调，以保留登录会话。

设置 MMG 用户目录中的 .env，或通过环境变量配置：

~~~dotenv
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=https://mmg.example.com
COOKIE_SECURE=true
~~~

Nginx 示例（证书、域名及允许的来源均为占位符）：

~~~nginx
server {
    listen 443 ssl;
    server_name mmg.example.com;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    # CIMD 文档必须允许授权服务器匿名 GET，不加登录或挑战页。
    location = /oauth/client-metadata.json {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        # 如需限制管理访问，在此加入 VPN / 来源限制；
        # 保证授权用户浏览器可访问 /api/v1/oauth/callback。
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
~~~

本机后端仍只监听 8765；Nginx 对外提供 443 和可信证书。不要仅做 HTTP 端口转发。访问 PUBLIC_URL 登录并发起授权，不要混用 localhost 与公网域名登录会话。修改 .env 后按部署流程重启 MMG；修改域名或回调后需重新授权。

无需登录访问：

~~~text
https://mmg.example.com/oauth/client-metadata.json
~~~

返回 JSON 中 client_id 必须与上述 URL 完全一致，redirect_uris 应包含 https://mmg.example.com/api/v1/oauth/callback。文档只包含公开客户端元数据，不包含 Client Secret、用户 Token 或 MMG 管理凭据。PUBLIC_URL 无法从外网访问、证书不被信任、或下游授权服务器不支持 CIMD 时，仍应选择预注册或符合条件的 DCR。

协议依据：[MCP 授权规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)、[客户端注册](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration)、[RFC 7591](https://www.rfc-editor.org/rfc/rfc7591.html)。
