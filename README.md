# MCP Manager Gateway

> Make MCP work like Skills: discover capabilities progressively, start servers only when needed, and give every agent one stable gateway.

[![PyPI version](https://img.shields.io/pypi/v/mcp-manager-gateway?label=PyPI)](https://pypi.org/project/mcp-manager-gateway/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-manager-gateway?label=Python)](https://pypi.org/project/mcp-manager-gateway/)
[![License](https://img.shields.io/github/license/eraycc/mcp-manager-gateway?label=License)](https://github.com/eraycc/mcp-manager-gateway/blob/master/LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/eraycc/mcp-manager-gateway?label=Release)](https://github.com/eraycc/mcp-manager-gateway/releases)
[![Docker Hub](https://img.shields.io/docker/v/eraycc/mcp-manager-gateway?label=Docker%20Hub)](https://hub.docker.com/r/eraycc/mcp-manager-gateway)
[![GHCR](https://img.shields.io/badge/GHCR-eraycc%2Fmcp--manager--gateway-blue)](https://ghcr.io/eraycc/mcp-manager-gateway)

English | [简体中文](https://github.com/eraycc/mcp-manager-gateway/blob/master/README.zh-CN.md) | [Documentation Wiki](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/README.md)

MCP servers are useful, but a large MCP configuration creates a new set of problems:

- every agent connects to many servers at startup, making startup slower;
- complete tool schemas consume context before the agent has done any work;
- rarely used servers stay alive and consume processes and connections;
- each client needs another copy of the same configuration;
- agents can understand existing configuration, but users still have to recreate services one by one.

MCP Manager Gateway, or **MMG**, puts those servers behind one gateway. An agent sees a small discovery surface, searches for the capability it needs, reads the exact schema, and calls the selected tool. Catalog operations do not start downstream services; a `lazy` server starts on its first real call.

~~~text
Agent / IDE
    │ configure MMG once
    ▼
MCP Manager Gateway
    ├── discover MCPs and tools without starting them
    ├── call an exact tool and start it only when required
    ├── manage users, tokens, permissions, OAuth, and lifecycle
    └── agent proposal → human review → real test → approval
            │
            ├── Filesystem MCP
            ├── Database MCP
            ├── Browser MCP
            └── other stdio / HTTP / SSE / REST services
~~~

## Why MMG

### Faster, smaller agent startup

On-demand discovery exposes three core gateway tools instead of injecting every downstream tool schema into the initial context. The agent first finds an MCP, then retrieves the full schema for a relevant tool, and finally invokes its exact gateway name.

This is progressive disclosure for MCP: reveal what exists first, then load detail only when it is useful. See [Progressive discovery](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/concepts/progressive-discovery.md).

### Real lazy loading and lazy startup

Listing MCPs, searching tools, and paging through the catalog never starts a downstream service. A `lazy` service starts on its first business call and can be reclaimed after an idle period. Frequently used services can be `eager`; unavailable services can be `disabled`.

### Configure once, reuse across agents

Each agent connects to MMG instead of carrying dozens of independent MCP definitions. The gateway centrally manages services, credentials, permissions, health, and runtime state. The same catalog can be safely assigned to different users and tokens.

For Web integrations with large catalogs, use [capability summary pages and exact definitions](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/catalog-api.md). Capability cards wrap long descriptions, URLs, and identifiers within their borders on desktop and mobile. Scheduled refresh retries enabled lazy/eager services even after a manual stop or startup failure and publishes the paged catalog. Stopping a service forcibly closes its current instances without disabling it or blocking later calls; a later call restarts a lazy/eager service, and an eager service then stays running. Calls and manual/scheduled catalog refreshes share one consecutive-failure counter per catalog owner. Below the configured threshold (default 3), MMG keeps the last successful four-kind catalog and its timestamp while exposing the failure reason. Only reaching the threshold empties the catalog and marks the owner failed. Any successful call or complete refresh resets the counter; connection establishment and token renewal alone do not. Internal retries count as one final outcome, and business calls are never replayed. Invalid authorization still blocks execution while retained catalog metadata remains visible to its authorized owner. The service policy is unchanged. Catalog queries remain read-only. Explicit exact calls may rediscover current contracts before validation and execution. Approved proposals discard diagnostic catalog entries and, like rejected proposals, no longer show the diagnostic catalog button. Approval tests support a bounded summary response; legacy full-list responses have explicit size limits. Agent service summaries include a bounded tool-name preview; enumerate `gateway_search_tools` for the full catalog. Exact calls use indexed routes and reuse schema validators.

Service, approval-test and profile capability cards show upstream tool annotation titles and green read-only, red destructive, blue idempotent and orange open-world badges. A badge appears only for the Boolean value `true`; missing or invalid hints are ignored. These are upstream declarations, not gateway guarantees. Refresh existing service catalogs or rerun approval tests to populate annotation summaries. MMG's six discovery tools explicitly declare all four Boolean hints; `gateway_call` conservatively declares potentially destructive, non-idempotent, open-world behavior. See [tool annotation display](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/catalog-api.md#工具注解展示).

Capability metadata summaries stay within 8 KiB. Oversized server icons, text, extensions and error lists are summarized with explicit omission/truncation markers, so display metadata cannot block catalog publication. Full tool definitions and input schemas remain intact.

### Observe and release active connections

The **Connections** page lists your business leases and their MCP instances; administrators can manage other users' leases. It supports filters, safe JSON details, confirmed single/batch disconnects, and five-second refresh that pauses in hidden tabs. Each lease shows current/queued calls, cumulative downstream business calls and the last dispatch time; short calls remain visible in these lease-lifetime counters. One or two instance entries use their natural height; three or more share a compact capped viewport, with scrolling for additional entries. Disconnect releases a permit and its sessions: unreferenced lazy instances drain and stop; eager instances remain, including session-isolated instances. Clients can reconnect; this does not revoke credentials. Settings provide independent instance idle reclamation and business lease validity (default 43,200 seconds; 0 disables lease expiry). The Bridge connects clients to MMG through stdio. Its heartbeats default to 90 seconds, with disconnection after three missed intervals. The heartbeat interval must be a positive integer (1–86,400 seconds). Setting the missed-heartbeat limit to 0 disables liveness detection; heartbeats continue at the configured interval for renewal and configuration sync, and lease validity remains independent. See [Connection management](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/manage-connections.md).

### Agent proposals with human approval

The recommended migration path is not to rebuild every MCP by hand. Give a trusted administrator token permission to submit MCP proposals, then ask an agent to:

1. Read existing Codex, Claude, generic JSON, or DSH/Cordis MCP configuration.
2. Normalize and submit multiple MCPs to MMG in one batch, up to 100 proposals.
3. Track proposal status.
4. Let a human inspect configuration, choose isolation and lifecycle policy, run a real test, and approve.
5. Use approved MCPs immediately through the gateway, without restarting connected agents.
6. Remove old client-side MCP entries only after successful validation and explicit user approval.

The agent prepares the migration; the human keeps the final decision. A proposal cannot choose privileged lifecycle or isolation fields and cannot bypass review. Read the full [MCP migration guide](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/migrate-mcps.md) and the agent contract in [AGENTS.md](https://github.com/eraycc/mcp-manager-gateway/blob/master/AGENTS.md).

## Install and start

MMG requires Python 3.12 or newer and is published on [PyPI](https://pypi.org/project/mcp-manager-gateway/).

### uv tool, recommended

~~~console
uv tool install mcp-manager-gateway
mmg
~~~

Upgrade a uv tool installation with:

~~~console
uv tool upgrade mcp-manager-gateway
~~~

### pip

Install into a dedicated virtual environment:

~~~console
pip install --upgrade mcp-manager-gateway
mmg
~~~

`mmg`, `mcp-manager`, and `mcp-manager-gateway` are equivalent commands. The default address is <http://127.0.0.1:8765>. The first registered account becomes the administrator; passwords must contain at least 10 characters.

Press Ctrl+C to stop the server. HTTP connections and request tasks have a 10-second grace period before cancellation, followed by application resource cleanup.

We recommend a Python build linked to SQLite with the WAL-reset fix: 3.51.3+, or the fixed 3.50.7/3.44.6 branches. Older engines remain usable and produce an upgrade warning. Check with `uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"`; upgrading uv alone does not replace an existing Python environment. See [runtime requirements](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/storage-governance.md#运行环境)。

On first start, MMG creates its configuration, SQLite database, and runtime data under:

- Windows: `%USERPROFILE%\.mcp-manager`
- Linux and macOS: `~/.mcp-manager`

To use another port:

~~~console
mmg serve --port 8766
~~~

### Docker (pull from registry)

Both registries publish the same multi-arch image (`linux/amd64`, `linux/arm64`):

- **Docker Hub** — `eraycc/mcp-manager-gateway` — <https://hub.docker.com/r/eraycc/mcp-manager-gateway>
- **GitHub Container Registry (GHCR)** — `ghcr.io/eraycc/mcp-manager-gateway` — <https://ghcr.io/eraycc/mcp-manager-gateway>

Pull and run from Docker Hub:

~~~console
docker pull eraycc/mcp-manager-gateway:latest
docker run -d --name mmg \
  -p 127.0.0.1:8765:8765 \
  -v mmg-data:/data \
  eraycc/mcp-manager-gateway
~~~

Pull and run from GHCR:

~~~console
docker pull ghcr.io/eraycc/mcp-manager-gateway:latest
docker run -d --name mmg \
  -p 127.0.0.1:8765:8765 \
  -v mmg-data:/data \
  ghcr.io/eraycc/mcp-manager-gateway
~~~

Pinning a specific release is recommended in production:

~~~console
docker run -d --name mmg \
  -p 127.0.0.1:8765:8765 \
  -v mmg-data:/data \
  eraycc/mcp-manager-gateway:1.3.9
~~~

Data is persisted in `/data` inside the container; map it to a named volume or host directory. See [Deployment](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/deployment-and-upgrades.md) for Compose files and production notes.

For source and container options, see [Installation](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/getting-started/installation.md) and [Quick start](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/getting-started/quick-start.md).

## Connect an agent

Create an access token in the Web console. The profile page generates copy-ready HTTP, stdio, Codex, and generic client configuration for the current gateway address.

The Streamable HTTP endpoint is:

~~~text
http://127.0.0.1:8765/mcp
Authorization: Bearer mcpm_YOUR_TOKEN
~~~

For clients that cannot connect to a remote HTTP MCP, use the stdio bridge:

~~~json
{
  "mcpServers": {
    "mcp-manager": {
      "command": "mmg",
      "args": ["stdio", "--url", "http://127.0.0.1:8765"],
      "env": {
        "MCP_MANAGER_TOKEN": "mcpm_YOUR_TOKEN"
      }
    }
  }
}
~~~

The bridge only connects the client to MMG. Downstream MCP processes still belong to the gateway and follow its lifecycle rules. See [Connect clients](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/connect-clients.md).

## Recommended migration workflow

1. Create an administrator token and enable the MCP proposal permission only for the migration.
2. Ask the agent to read the current client configuration and batch-submit proposals without changing the source file.
3. Review each proposal under **MCP Approvals**. Check commands, URLs, environment variables, directory access, and credentials.
4. Choose `lazy`, `eager`, or `disabled`, select the required isolation, and run a real test.
5. Approve the service, then verify discovery and one representative call through MMG.
6. After every target is approved and working, explicitly authorize the agent to remove only the migrated entries from the old configuration.

A useful instruction for an agent is:

> Read the MCP configuration in my current client, normalize it, and batch-submit it as MMG proposals. Do not edit the original configuration. Wait for my review, testing, and approval. Remove migrated entries only after I explicitly approve cleanup.

Keep a backup, preserve unrelated settings, and never remove the MMG connection itself.

## Progressive tool use

| Tool | Purpose |
| --- | --- |
| `gateway_search_mcps` | Find MCPs visible to the current token |
| `gateway_search_tools` | Return the full `inputSchema` and exact `gateway_name` |
| `gateway_call` | Invoke a tool with its exact name and schema-valid arguments |
| `gateway_search_resources` | Optionally search resource, prompt, and template metadata with filters, matching modes, semantic retrieval and stable pagination (default 20 per page, maximum 100) |
| `gateway_read_resource` | Optionally read an exact resource or prompt URI; prompts accept string `arguments` |
| `gateway_mcp_proposals` | Optionally submit or inspect proposals with an authorized administrator token |

Resource search and reading remain an optional pair controlled by the token editor’s resource-tools checkbox; existing enabled tokens require no migration. Restart or reconnect clients after upgrading to refresh the advertised tool names. Gateway tools use concise parameter hints and one minimal `inputSchema.examples` example each; OAuth proposals describe all four `config.auth.auth_mode` values.

Reliable agents do not guess tool names or arguments: search for the MCP, retrieve the tool schema, and call the exact returned name. Discovery and pagination do not wake lazy services. See the [Gateway tool reference](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/gateway-tools.md). Failed services report their state without advertising executable tools; keyword search supports bounded one-character typo recovery.

Tool-embedded resources (including GitHub MCP file contents) are preserved and exposed as bounded session snapshots through native resource methods and the optional resource tools. Resource discovery supports `kind`, `match` and source `uri_prefix` filters, with background vectors for static resources, prompts and templates. The default-off `embedded_resource.inject` switch under Settings → Tool search → Resource compatibility enables a fallback for clients that cannot consume embedded resources or services without an equivalent readable resource template. Disabling it stops capture and clears all temporary snapshots; re-enabling requires another tool call. Temporary resources appear before static entries, newest first, and support source URI substring/token searches. Authenticated stateless requests reuse their existing implicit retention lease; use separate tokens or explicit leases when agents need independent snapshots. HTTP calls mirror `x-mcp-header` parameters from the authorized cached schema. Call logs retain the original downstream `result` and record only added gateway content, such as temporary resource URI hints, under `gateway_processing.added_content`.

The separate default-on `exposeNativeResources` switch in the same settings group hides only native `resources/list`, `prompts/list` and `resources/templates/list` when disabled, including temporary snapshot listings. Native `resources/read` and `prompts/get` remain available for known identifiers under existing authorization. The token-level resource tools option independently controls `gateway_search_resources` and `gateway_read_resource`; neither switch changes the other. See [Embedded resource compatibility](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/embedded-resources.md) for limits, isolation and keyword fallback behavior.

## Web console languages

MMG supports optional browser-side interface translation:

1. An administrator opens **System Settings → Translation Settings**.
2. Enable **Global Web Translation**, choose the source language, default target language, and translation service, then save.
3. A language control appears in the top-right corner, initially selecting the configured default when this browser has no saved choice.
4. Choose the actual target language there; the current page is translated immediately.

The language selected from the top-right control is saved in the current browser profile and takes priority over the system default. On later visits and sign-ins, MMG automatically applies that saved selection while translation remains enabled. The default target language only initializes browsers that have no local choice, so different browsers can choose independently.

Browser translation can send visible page text to the configured translation provider. Use a reviewed private provider for sensitive deployments, or keep translation disabled. Configuration, cache controls, and provider details are covered in [Interface translation and update checks](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/interface-translation-and-updates.md).

## Administration overview

- stdio, Streamable HTTP, legacy SSE, and REST-to-MCP transports;
- newly created, imported, approved and copied services automatically attempt to publish their paged capability catalogs;
- generic JSON, Codex TOML, Claude, and DSH Cordis/registry import; exports use common `mcpServers` connection fields with a versioned MMG `config` extension for lossless re-import ([format](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/catalog-api.md#service-configuration-export));
- console export of all service configurations as redacted JSON after confirmation; cross-page selection and batch actions in service, approval, token, log, and user lists ([console guide](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/manage-services.md));
- cached tools, resources, prompts, and resource-template counts in service lists and accessible profile services, with service descriptions;
- persistent profile credential editors for user-isolated Bearer, Basic, API Key, and private OAuth services, for administrators and regular users; authenticated HTTP users can refresh only their own capability cache from the credential or OAuth dialog. Administrator service lists show the administrator’s own capabilities and configured-user totals for enabled users with service access ([guide](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/manage-services.md#个人凭据配置与修改));
- independent user, token, anonymous-scope, and MCP assignment controls;
- `lazy`, `eager`, and `disabled` lifecycle modes;
- shared, per-user, and per-session runtime isolation;
- OAuth isolation for personal credentials and runtime instances;
- call logs, audit logs, background jobs, caches, and failure diagnostics; administrator audit management records each scheduled catalog refresh with its MCP, owner scope and outcome, as well as automatic instance stops and expired lease releases linked by lease and operation IDs, and includes filtered inspection, JSON/CSV export, confirmed background cleanup, independent call/audit retention policies (both default to unlimited), and recoverable index rebuilding ([audit guide](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/audit-logs.md));
- light, dark, and system themes;
- optional translation with provider and ignore-rule controls;
- optional PyPI update detection: the About page's automatic check interval sets the lifetime of cached PyPI latest-version information, defaulting to 86,400 seconds (24 hours). After that time has elapsed since the last successful check, the next login, About-page visit or refresh queries PyPI; otherwise it reads the cache. Check results update the mounted About panel and both notification dots without a page reload. Manual checks always query PyPI. The interval remains editable and can be saved when automatic checks are disabled;
- keyword and fuzzy tool search, with optional OpenAI-compatible embeddings;
- SQLite by default and optional MySQL;
- a compact primary database, one `indexes/derived.sqlite` projection, separate durable/cache CAS objects, and daily `logs/calls/` JSONL sources; configurable storage budgets, background collection, and independent SQLite compaction ([storage governance](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/storage-governance.md)).

See [Manage services](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/manage-services.md), [Runtime and lifecycle](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/concepts/runtime-and-lifecycle.md), and [Security model](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/security/security-model.md).

## OAuth client registration

Remote HTTP/SSE OAuth uses one four-option registration selector: pre-registration (Client ID required, Secret optional), prefer CIMD with DCR fallback (the default for new forms), CIMD only, or DCR only. The preferred mode falls back when CIMD prerequisites are unavailable or a validated authorization response explicitly rejects the client or its metadata; DCR requires a registration endpoint. Legacy settings retain their selected policy. Open the OAuth dialog and click Start authorization; opening it alone does not register a client, but renews an expired token when refresh credentials are available.

Service management and profile OAuth dialogs both offer **Reset authorization**, with a second confirmation that existing tokens will be kept. Reset clears pending authorization states and cached DCR registration only for the selected MCP and authorization scope (only your own under user isolation, including shared configuration); it preserves CIMD verification, existing tokens, and their refresh client credentials. Close old authorization tabs, reset, then start authorization again. Each new attempt replaces pending states in the same scope; states expire after 10 minutes and can be used once. Profile dialogs load the latest shared configuration as a read-only summary or your latest private configuration.

CIMD supports two metadata hosting options. For a local/private MMG, choose **External static hosting**, enter a public HTTPS JSON URL, generate and publish the file, then verify it. Only this credential-free file needs public inbound access; MMG can stay private. The callback remains `PUBLIC_URL` plus the selected path, must exactly match an entry in the document, and must be reachable by the same signed-in browser. HTTP loopback works only when browser and MMG are on the same machine; mobile/LAN access needs a browser-trusted HTTPS address. For a public HTTPS gateway, keep **MMG hosting** at `/oauth/client-metadata.json`. Both options require a compatible authorization server; verification is not successful authorization. Expired tokens are renewed before connection and catalog scopes are materialized. Authorization callbacks, normal calls, manual/scheduled catalog refreshes and profile capability refreshes share catalog preparation. A credential conflict during preparation, read-only discovery or publication retries once with the latest credentials; successful renewal completes the same operation. Exact tool routing is resolved again before business execution. Concurrent sessions sharing a credential coalesce renewal; user-isolated credentials remain separate. A replacement refresh token is encrypted and saved with the new access token before use; if omitted, the existing refresh token is retained. The OAuth dialog renews expired tokens when checking status and offers Refresh authorization when a refresh token is available. Refresh failure or a downstream HTTP 401 requires explicit reauthorization, without replaying business calls. See the [OAuth and private MMG setup guide](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/oauth.md).

All registration policies expose Token endpoint authentication: automatic, none, client_secret_basic, or client_secret_post. Automatic DCR negotiates server-supported methods and securely stores a generated Secret for token exchange and refresh. CIMD currently uses none; an explicit Secret method triggers DCR fallback in preferred mode or a configuration error in CIMD-only mode. A fallback starts a fresh state/PKCE authorization flow once; user denial, invalid state/issuer, and uncertain token requests never trigger it. If the provider shows an error page without returning to MMG, select DCR only and retry. See the OAuth guide above for the bounded Figma compatibility behavior. OAuth forms expose a Callback path selector: `/api/v1/oauth/callback` by default, or `/callback` for services such as Figma. Both manual and automatic registration use the selected path on `PUBLIC_URL`; changing it invalidates the related registration and tokens and requires reauthorization. No provider URL matching selects the callback path. DCR failures include recognized server error codes without echoing response bodies or secrets.

## Configuration and deployment

Environment variables override the user-home `.env`. MMG does not automatically load `.env` from the source tree or current working directory. Start from [.env.example](https://github.com/eraycc/mcp-manager-gateway/blob/master/.env.example).

~~~dotenv
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=http://127.0.0.1:8765
COOKIE_SECURE=false
DATABASE_URL=
~~~

Use `MCP_MANAGER_HOME` or `mmg --home /path serve` to select another configuration and data directory. Back up the complete data directory before upgrades or migration.

Start the included container deployment with:

~~~console
docker compose up --build -d
~~~

The default bind address is local-only. For remote or production use, place MMG behind an HTTPS reverse proxy and set `PUBLIC_URL` and `COOKIE_SECURE=true` correctly. MMG is currently designed as a single gateway runtime; do not share one downstream process scheduler across multiple workers or replicas.

Read [Database deployment, migration, and backup](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/database-deployment.md), [Configuration](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/configuration.md), [Deployment and upgrades](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/deployment-and-upgrades.md), and [Backup and migration](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/backup-and-migration.md).

## Upgrades

Upgrade with the mechanism that installed MMG:

~~~console
# uv tool
uv tool upgrade mcp-manager-gateway

# pip virtual environment
pip install --upgrade mcp-manager-gateway
~~~

Source and container installations should be updated through Git plus uv or by rebuilding/pulling the relevant image. `mmg upgrade` performs database schema migration; it does not update the installed Python package.

A bounded background collector reclaims expired query caches, retired index rows, and unreferenced objects. Query caches have configurable capacity and TTL; current catalog vectors remain tied to their authorized generation and model. Unchanged tools reuse existing same-scope vectors without requesting the model again; disabled services and failed discovery scopes are preserved. Legacy `jobs/jobs.jsonl` is no longer read or imported and is not automatically deleted. See [Backup and migration](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/backup-and-migration.md#缓存与历史残留).

Update detection in **System Settings → About** only reports availability. It never modifies the installation. Automatic checks can be disabled while the manual **Check for Updates** action remains available.

## Development

~~~console
uv sync --frozen --group dev
uv run pytest tests -q
node --test tests/frontend/core.test.mjs
uv build
~~~

Daily `uv run pytest -q` runs functional regressions, including browser and safety-boundary tests, with up to four workers. Long performance tests and live external-service tests require explicit selection. Use `--suite quick`, `--suite backend`, `--suite browser`, `--domain oauth`, or `--changed-from <GIT_REF>` for focused work; `-n 0` forces serial execution. `--suite performance` explicitly runs stress tests serially by default; `--suite all` includes performance but still requires `--run-live` for external services. Each run reports setup/call/teardown timings. See [test suites and coverage-preserving optimization](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/development/testing.md).

Contributor guidance is in [Contributing](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/development/contributing.md); transport extension details are in [Transport plugins](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/development/transport-plugins.md).

## Documentation map

- [Documentation Wiki](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/README.md): the public documentation index
- [Installation](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/getting-started/installation.md) and [Quick start](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/getting-started/quick-start.md)
- [Client connections](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/connect-clients.md) and [MCP migration](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/guides/migrate-mcps.md)
- [Progressive discovery](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/concepts/progressive-discovery.md) and [Runtime lifecycle](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/concepts/runtime-and-lifecycle.md)
- [Database deployment](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/database-deployment.md), [Configuration](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/configuration.md), [Deployment](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/operations/deployment-and-upgrades.md), and [Troubleshooting](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/troubleshooting/common-issues.md)
- [CLI reference](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/cli.md) and [Gateway tool reference](https://github.com/eraycc/mcp-manager-gateway/blob/master/wiki/reference/gateway-tools.md)
- [Agent instructions](https://github.com/eraycc/mcp-manager-gateway/blob/master/AGENTS.md)
- [简体中文 README](https://github.com/eraycc/mcp-manager-gateway/blob/master/README.zh-CN.md)

## Security

MCP configuration can start local processes, access networks, and read data. Treat service administration as privileged. Review proposed commands and permissions, use least-privilege accounts and tokens, isolate different trust domains, and verify arguments before invoking tools with side effects.

An `outcome_unknown` result means a request may already have reached its destination. Agents must not automatically replay it.

## License

Copyright 2026 eraycc. Licensed under the [Apache License 2.0](https://github.com/eraycc/mcp-manager-gateway/blob/master/LICENSE).

Large catalogs support progressive `gateway_search_tools` with `names_only: true` (no Schema reads). Query exact names again without that flag before calling. Follow `next_cursor` without changing query arguments. Native tool/resource/prompt/template listings and `gateway_search_resources` paginate; complete MCP JSON-RPC envelopes are budgeted at 1 MiB including text and structured duplication. An individually oversized complete contract returns `catalog_entry_too_large`, never a truncated Schema.
