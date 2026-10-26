# OAuth 预注册、CIMD 与 DCR

[文档首页](../README.md) · [English README](../../README.md) · [简体中文 README](../../README.zh-CN.md) · [服务管理](manage-services.md)

MMG 作为 OAuth 客户端访问下游 MCP；这与 Agent 登录 MMG 所使用的访问 Token 相互独立。新增、编辑、审批和个人独享配置使用相同字段规则。

## 选择注册方式

| 方式 | 填写内容 | 使用条件 |
| --- | --- | --- |
| 预注册 | Client ID 必填；Secret 可选；可填写授权、Token 地址与 Scopes | 已在服务提供方创建客户端；端点留空时尝试发现 |
| 自动（优先 CIMD，失败回退 DCR）（新表单默认） | 无需手填 Client ID/Secret；DCR 客户端名称默认 Codex | CIMD 不可用时回退；服务器必须提供 registration_endpoint 才能使用 DCR |
| 自动（仅 CIMD） | 无需手填 Client ID/Secret | 服务器声明支持 CIMD，且所选托管方式可用；可用外部静态文件保持 MMG 私有，失败不回退 |
| 自动（仅 DCR） | 客户端名称默认 Codex，可修改 | 直接使用 registration_endpoint，不尝试 CIMD |

预注册使用手动填写的 Client ID/Secret，不发起自动注册。CIMD 以公网 HTTPS 元数据 URL 作为客户端标识，可由 MMG 或外部静态站点托管。外部静态托管适用于本机和内网部署，不要求 MMG 本体公开。DCR 是动态客户端注册，MMG 直接向服务器注册并获取 Client ID/Secret，不要求 MMG 有公网地址；所有方式的回调仍须被浏览器访问并被提供方接受。

“优先 CIMD”模式在服务器未声明支持、所选元数据托管不可用或所选 Token 认证方式与 CIMD 不兼容时回退 DCR，并记录脱敏结构化审计事件 `oauth.cimd_fallback`（原因、阶段与结果）。客户端名称配置只用于 DCR 注册请求，默认 Codex；CIMD 与预注册的授权请求不会发送该字段。CIMD 公共文档使用 MMG 固定名称。填写 Codex 不会取得官方客户端的身份或特权；Figma 等服务可能要求特定名称或回调路径。

实际 CIMD 授权失败后，仅在已通过用户、会话、state、issuer、配置、资源、回调与授权代次校验的响应中，收到 `invalid_client`、`invalid_client_metadata` 或 `unauthorized_client` 时回退。Token 端点的上述明确拒绝还要求 HTTP 400/401。回退最多一次，并使用新的 state、PKCE 和授权码，绝不重复交换旧授权码。用户拒绝、通用协议错误、Token 超时或结果不明不会触发回退。若浏览器停留在提供方错误页、未回调 MMG，无法自动检测，请选择“仅 DCR”重试。“仅 CIMD”始终不回退；没有动态注册端点时会提示改用预注册。

字段 `auth.auth_mode` 保存为 `pre_registration`、`prefer_cimd_fallback_dcr`、`cimd_only` 或 `dcr_only`；不再保存独立的 `allow_dcr` 开关。旧值兼容读取，并在保存时规范化：`manual` 对应预注册，`auto + allow_dcr=true` 对应优先 CIMD，旧 `auto` 未开启回退对应仅 CIMD，`dcr` 对应仅 DCR。历史未指定模式的端点/Token 配置保持原预注册语义；等价设置与有效 DCR 注册缓存兼容，不因字段改名清除授权。

切换模式时，不适用的 Client ID/Secret 和客户端名称不会提交；保留用户选择的 Token 端点认证方式和回调路径。新增、编辑、审批与个人独享配置均可设置以下选项：

| Token 端点认证方式 | 预注册 | 自动注册 |
| --- | --- | --- |
| 自动选择 | 有 Secret 时按服务器能力选择 Basic/Post，无 Secret 时使用 none | CIMD 使用 none；DCR 按服务器声明依次选择 none、client_secret_post、client_secret_basic |
| none | 不填写 Secret，配合 PKCE | 请求公开客户端注册；不会发送注册响应中多余的 Secret |
| client_secret_basic | 必须填写 Secret，使用 HTTP Basic | DCR 必须返回有效 Secret，使用 HTTP Basic |
| client_secret_post | 必须填写 Secret，将其放入表单请求体 | DCR 必须返回有效 Secret，将其放入表单请求体 |

每次 Token 请求只使用一种认证方式，不在失败后自动改用另一种方式或重复交换授权码。显式选择的方式必须受服务器支持；不支持时在注册前提示。CIMD 的公开文档固定为 none；显式选择 Secret 方式时，优先模式回退 DCR，仅 CIMD 模式提示不兼容。

DCR 返回的 Client ID、Secret、认证方式和可选 Secret 有效期按配置归属加密缓存，不回填到可编辑的服务配置，也不出现在授权 URL、公开元数据或审计日志中。有效注册可复用；更改认证方式使旧注册及待处理授权失效；重新注册取得不同的 Client ID、Secret 或认证方式时，相关配置作用域内的旧授权一并作废，避免用新客户端刷新旧 Token。Secret 缺失、非法或过期时停止并要求重新授权；注册只在用户明确开始授权及该授权内的一次 CIMD 回退中发生，业务调用不自动注册。

Figma 的限定兼容：当发现的 issuer、资源、注册端点和 Token 端点均为其已知官方地址，且已协商 Secret 认证、注册响应却声明 none 并返回 Secret 时，MMG 按请求的 Secret 认证方式处理。其他服务器的矛盾响应不套用此规则。Figma 场景可以选择 client_secret_post；名称、端点、回调、权限及用户授权仍须由服务器接受，实际联调成功才代表可用。

OAuth 回调路径可在服务编辑、提议审批和个人凭据配置中选择，配置字段为 `auth.callback_path`。默认 `/api/v1/oauth/callback`，可选 `/callback`；MMG 使用 `PUBLIC_URL + 所选路径` 生成完整回调地址，预注册、CIMD 和 DCR 均遵循此设置，不再按服务 URL 自动切换。

OAuth 回调路径。大多数服务使用默认的 `/api/v1/oauth/callback`；Figma 等部分远程 MCP 服务要求使用 `/callback`，若授权时提示 `invalid_redirect_uri`，请切换到 `/callback` 重试。

更改路径会清除对应配置作用域内的旧注册信息和 Token，并作废待处理授权；保存后重新开始授权。共享配置变更会影响共享此配置的用户，个人配置只影响本人。预注册客户端还须同步更新服务端登记的回调地址。仅接受上述两个已实现的路径，不接受完整外部 URL、任意路径、查询参数或片段。未配置与显式选择默认路径等价。

`/callback` 与原回调共用登录用户、会话、state、PKCE、issuer、资源及授权代次校验；使用与 PUBLIC_URL 相同的主机名登录，避免切换 localhost 与 127.0.0.1 导致会话丢失。注册、授权链接、交换授权码和缓存绑定使用同一回调地址，不在失败后悄悄更换地址重试。

注册失败时，提示保留 HTTP 状态并仅显示允许的 OAuth 错误码（如 `invalid_redirect_uri`、`invalid_client_metadata`），不回显任意 error_description、响应正文或密钥。Figma 的服务器准入规则仍适用；DCR 成功只代表客户端已注册，仍需在浏览器完成用户授权。

## 发现、授权与缓存

Scopes 用于指定申请的访问权限，多个值按 OAuth 2.0 标准以空格分隔，例如 `mcp:connect profile:read`；实际值以服务提供方要求为准。`mcp:connect` 是 Figma 等服务的常见值，可自行修改；进入端点自动发现流程时，留空则使用发现的权限信息，不强制给所有 MCP 填入相同值。

先发现受保护资源元数据，再发现所选授权服务器的 OAuth/OIDC 元数据；发现支持 401 challenge、路径及根 well-known 地址。端点使用 HTTPS，本机回环可使用 HTTP；不跟随元数据重定向，不转发 MMG 或下游凭据。公共 MCP 引导的请求会校验 DNS 解析地址，并固定连接到已校验的公共 IP，保留 TLS 主机名验证。单次元数据/注册请求有 8 秒总时限，Token 请求有 30 秒总时限，JSON 响应上限 256 KiB。

1. 保存配置。此时不执行 DCR。
2. 从服务管理或个人资料打开共用的“授权”弹窗，查看当前用户状态。个人资料读取最新配置：共享配置显示只读摘要，无需重新填写；个人独享配置显示本人最新保存的内容。
3. 明确点击“开始授权”或“重新授权”，才发现端点、选择 CIMD 或注册 DCR，并生成 PKCE 授权链接。
4. 点击“打开授权页面”，在服务提供方确认权限。
5. 返回后重新检查授权状态并发现能力；禁用的服务不会因此自动启用。

DCR 注册信息加密缓存，按 MCP、配置归属、实际资源地址、issuer、注册端点和回调地址绑定。共享配置可以复用注册客户端，各用户 Token 仍分别保存；个人独享配置单独注册。更改配置、资源地址或回调地址会要求重新授权；issuer 改变会作废相关旧授权。环境变量形式的资源 URL 在授权、回调与使用时按实际展开地址核对。DCR 网络失败可能已在对方创建客户端，MMG 不自动重试，应检查对方记录后再手动发起。

每次开始授权会替换同一授权作用域内的旧待完成 state：共享授权覆盖当前 MCP；用户隔离授权仅影响当前用户，即使采用共享配置。回调绑定原用户、会话、服务版本、授权代次与 10 分钟有效、单次使用的 state，使用 PKCE S256，并核对所发现 issuer 的 iss 响应要求。授权和 Token 请求携带绑定的 resource。并发回调或旧请求的延迟 401 不能覆盖新授权。

回调失败会区分 state 缺失、已过期、已使用或绑定不匹配，便于判断应重新开始授权还是检查登录会话和配置。失败回调与 state 作废分别记录 `oauth.callback_failed`、`oauth.state_invalidated` 审计；日志仅记录 state 前 8 位，不记录完整值。

## Token 状态与恢复

- valid：Token 尚未进入过期窗口；未提供 expires_in 时视为有效期未知，直到下游拒绝。
- expired：Token 已过期或将在 60 秒内过期；有 Refresh Token 时，正常调用、目录刷新及 OAuth 状态检查先尝试续期，成功后继续本轮操作。
- needs_reauth：无有效授权、无可用刷新凭据、刷新失败或下游明确返回 HTTP 401。

授权成功后的目录发现、工具调用前的目录恢复、管理员手动刷新、定时目录刷新，以及“个人资料 → 可用服务”中的本人能力刷新，共用“续期 → 重读凭据及作用域 → 只读发现 → 校验并发布”的流程。作用域在续期完成后固定；准备、入库或发布阶段遇到 `credential_changed`，丢弃本次过时快照并最多同轮重试一次，重新校验权限、读取最新凭据并完成只读发现。重试不会强制再消费已轮换的 Refresh Token。成功后在同一轮完成发布，无需再次点击或再次触发任务；持续冲突或权限撤销仍失败关闭。Agent 在目录恢复后重新解析精确工具路由，再固定业务调用的作用域；业务调用开始后的凭据变更及结果不明均不会触发重放。

同一 MCP 的服务共享凭据按服务串行合并续期；按用户隔离时按 MCP 与用户分别合并，即使 OAuth 配置共享，各用户 Token 仍相互独立。同一轮并发手动“刷新授权”也会复用已完成的续期，避免重复使用旧 Refresh Token。OAuth 弹窗检查到过期时先续期，存在刷新凭据时提供“刷新授权”按钮；状态检查和此按钮不启动下游 MCP，也不自动注册客户端。状态接口 `GET /api/v1/mcps/<server-id>/oauth/status` 返回 `refresh_available`；手动续期使用 `POST /api/v1/mcps/<server-id>/oauth/refresh`，沿用当前登录用户、服务归属和 CSRF 校验。

Refresh Token 轮换与 Access Token 更新在同一数据库事务中加密保存。服务提供方返回新的 `refresh_token` 时替换旧值，下一次续期只使用新值；响应省略该字段时保留原值。凭据按服务或用户隔离保存，续期成功后再读取连接配置与目录作用域，避免使用续期前的快照发布目录。

定时刷新不依赖用户打开 OAuth 弹窗：服务共享模式使用服务凭据；按用户隔离时只刷新仍有访问权的启用用户各自的目录，支持共享或个人独享 OAuth 配置。即使没有初始能力缓存，也会先续期再发现。排查时查看“后台任务”中 `mcp.scheduled_refresh` 的逐项结果；某个服务失败不会阻止其他服务完成。`Catalog credential scope changed` 是凭据快照校验冲突，不等于服务提供方拒绝了 Refresh Token。升级后应确认 MMG 服务进程已重启，并以新任务结果判断修复是否生效；历史失败记录保留，不会因后续成功而改写。

刷新失败及 401 会持久记录“需要重新授权”；重启、定时发现和再次调用都不会继续自动刷新，更不会自动重新注册客户端或重放业务调用。重新授权成功后清除终态。断开授权清除 Token，保留可复用的注册信息，不代表撤销服务提供方的授权；需要彻底撤销时请同时到提供方操作。

授权流程卡住或旧链接失效时，可使用“重置授权”：

1. 关闭仍在等待回调的旧授权标签页。
2. 在服务管理或个人资料的 OAuth 弹窗点击“重置授权”，阅读二次确认：只清理当前 MCP、对应授权作用域的待完成 state 与 DCR 注册缓存；用户隔离时只影响本人。
3. 确认后重新点击“开始授权”或“重新授权”，使用新链接完成授权。

重置保留 CIMD 验证、已有 Token 及其刷新所需的原客户端凭据，不注销服务提供方会话，也不撤销已有 Token。它与“断开授权”用途不同，无需重启浏览器。下一次明确开始授权时按当前配置重新选择或注册客户端；如果实际配置或客户端身份改变，仍适用上文的授权失效规则。

## 本机／内网 MMG：外部静态托管

新增、编辑、提议审批和个人独享 OAuth 配置，在回调路径与 Scopes 后共用同一个 CIMD 托管区域。仅优先 CIMD 和仅 CIMD 显示；切换到 DCR 或预注册时隐藏并保留内容。桌面和手机采用相同操作顺序。

1. 先确定浏览器可访问的 MMG `PUBLIC_URL` 和回调路径。
2. 选择“外部静态托管”，填写长期稳定、匿名可读、直接返回 HTTP 200 JSON 的 HTTPS URL，例如 `https://metadata.example.com/mmg.json`。文件名任意；URL 必须包含路径，不允许凭据、fragment 或点路径，不支持跳转链接。
3. 点击“生成 JSON”，复制或下载文件，发布到自有 HTTPS 静态站点（例如配置正确的 Pages 站点）。文件使用 `Content-Type: application/json`；仓库网页、登录页或返回 `text/plain` 的 raw 链接不能通过验证。MMG 不代为上传。
4. 可展开预览修改名称、主页等公开字段。`client_id` 必须与托管 URL 逐字符相同，`redirect_uris` 必须包含界面显示的 **MMG 实际回调地址**。编辑这里不会改变 MMG 发送的回调地址；需要更改主机时先修改 `PUBLIC_URL` 并重启。
5. 点击“验证线上文件”，检查的是真实线上响应。已有编辑内容时还须与线上文档一致；修改托管 URL、回调或 JSON 后须重新发布、验证。保存不会执行 DCR 或开始用户授权。
6. 保存后明确开始授权。尚未验证、超过 24 小时或绑定配置变化时，会自动验证；优先模式失败按原策略回退 DCR，仅 CIMD 模式报错，绝不悄悄换回 MMG 托管。

同机部署示例：

~~~dotenv
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=http://127.0.0.1:8765
~~~

对应公开文件示例（只将此文件放在公网）：

~~~json
{
  "client_id": "https://metadata.example.com/mmg.json",
  "client_name": "MCP Manager Gateway",
  "redirect_uris": ["http://127.0.0.1:8765/api/v1/oauth/callback"],
  "grant_types": ["authorization_code"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
~~~

访问关系：授权服务器从公网获取 JSON；用户浏览器重定向到本机／内网 MMG；MMG 出站访问授权服务器完成 Token 交换。**授权服务器不需要直接连接 MMG 的回调。** 此功能不提供回调代理、内网穿透，也不会让云端 Agent 自动连通私有 MMG。

| 使用场景 | 实际回调建议 | 条件 |
| --- | --- | --- |
| 浏览器与 MMG 同一电脑 | `http://127.0.0.1:8765/…` | 使用同一浏览器和同一主机名登录、授权 |
| 手机访问电脑／内网服务器 | `https://mmg.internal.example.com/…` | 手机可经局域网或 VPN 访问；证书受浏览器信任 |
| 公网共享网关 | 公网 HTTPS 地址 | 可选择 MMG 托管，详见下节 |

手机上的 `127.0.0.1` 指向手机，不指向运行 MMG 的电脑。不要在电脑开始授权后把链接转到手机或另一浏览器；现有回调校验绑定原登录会话。MMG 不接受 HTTP 内网 IP 作为此功能的回调，不把 `0.0.0.0` 当作访问地址；它只是监听地址。HTTPS 回调可以经私有网络访问，无需将 MMG 暴露公网。

验证要求完整 `redirect_uri` 一致，不能仅比较路径；不同协议、主机、端口或尾斜杠均可能导致下游拒绝。提供方可能进一步限制回环地址、内网地址、客户端域名或认证方式，MMG 本地验证不能替代真实联调。不宣称任意服务均兼容。

配置字段为 `auth.cimd_metadata_source`（缺省 `mmg`，可选 `external`）、`auth.cimd_external_url` 和可选编辑草稿 `auth.cimd_metadata`。验证证据由服务器单独加密保存，按当前用户、完整托管 URL、实际回调和文档摘要核对；不接受表单中的伪造验证时间。每用户最多保留 32 个最近验证结果；结果被淘汰后授权时重新验证，不影响已有 Token。非 CIMD 模式忽略保留的托管字段。验证状态刷新不会清除有效授权。

验证请求不带 MMG Cookie、Token 或下游凭据；校验 DNS 的全部解析地址并固定实际连接 IP，禁止回环、私有网络和链路本地目标，不跟随 HTTP 跳转。总时限 5 秒、读取上限 64 KiB、每用户每分钟最多 5 次线上验证；状态查询不访问外网。结果使用结构化错误码，例如 `client_id_mismatch`、`redirect_uri_mismatch`、`invalid_content_type`、`redirect_not_allowed`、`unsafe_address`、`document_mismatch`。审计事件 `oauth.cimd_verify` 只记录绑定摘要、状态和错误码。

静态文件公开的是客户端标识、名称和回调位置，可能透露内部主机名；只发布必要字段。禁止放入 Secret、Token、私钥，当前实现使用 `none + PKCE`。公网文件和域名仍须长期维护，托管账户被入侵可导致元数据被篡改；周期验证不能保证实时检测或证明域名所有权。下游凭据继续留在 MMG，仍按已有服务／用户作用域加密保存。

## 公网 HTTPS 与 Nginx（MMG 托管）

选择 MMG 托管时，CIMD 的 Client ID 是 MMG 自身的 HTTPS 元数据 URL。授权服务器需要从公网读取它；不要求把管理页面开放给所有人。浏览器也必须能够访问同一 PUBLIC_URL 的控制台与回调，以保留登录会话。

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
        # 保证授权用户浏览器可访问 /api/v1/oauth/callback 和 /callback。
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

返回 JSON 中 client_id 必须与上述 URL 完全一致，redirect_uris 同时声明 https://mmg.example.com/api/v1/oauth/callback 和 https://mmg.example.com/callback，使授权服务器可校验所选路径。文档只包含公开客户端元数据，不包含 Client Secret、用户 Token 或 MMG 管理凭据。MMG 托管地址无法从外网访问时，可改用外部静态托管。两种方案的元数据 HTTPS 证书均须受授权服务器信任，实际回调须由浏览器访问；下游不支持 CIMD 时，应选择预注册或符合条件的 DCR。

协议依据：[OAuth CIMD 草案（当前核对版本 -02）](https://www.ietf.org/archive/id/draft-ietf-oauth-client-id-metadata-document-02.html)、[MCP 授权规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)、[客户端注册](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration)、[RFC 7591](https://www.rfc-editor.org/rfc/rfc7591.html)。
