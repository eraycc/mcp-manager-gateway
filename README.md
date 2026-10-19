# MCP Manager Gateway

> Make MCP work like Skills: discover capabilities progressively, start servers only when needed, and give every agent one stable gateway.

[![PyPI](https://img.shields.io/pypi/v/mcp-manager-gateway)](https://pypi.org/project/mcp-manager-gateway/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-manager-gateway)](https://pypi.org/project/mcp-manager-gateway/)
[![License](https://img.shields.io/github/license/eraycc/mcp-manager-gateway)](LICENSE)

English | [简体中文](README.zh-CN.md) | [Documentation Wiki](wiki/README.md)

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

This is progressive disclosure for MCP: reveal what exists first, then load detail only when it is useful. See [Progressive discovery](wiki/concepts/progressive-discovery.md).

### Real lazy loading and lazy startup

Listing MCPs, searching tools, and paging through the catalog never starts a downstream service. A `lazy` service starts on its first business call and can be reclaimed after an idle period. Frequently used services can be `eager`; unavailable services can be `disabled`.

### Configure once, reuse across agents

Each agent connects to MMG instead of carrying dozens of independent MCP definitions. The gateway centrally manages services, credentials, permissions, health, and runtime state. The same catalog can be safely assigned to different users and tokens.

For Web integrations with large catalogs, use [capability summary pages and exact definitions](wiki/reference/catalog-api.md). Capability cards wrap long descriptions, URLs, and identifiers within their borders on desktop and mobile. Scheduled refresh retries enabled lazy/eager services even after a manual stop or startup failure and publishes the paged catalog. Stopping a service forcibly closes its current instances without disabling it or blocking later calls; a later call restarts a lazy/eager service, and an eager service then stays running. A failed refresh empties the catalog and persists its error for agents and the console; it never disables an enabled service. Catalog queries remain read-only. Explicit exact calls may rediscover current contracts before validation and execution. Approved proposals discard diagnostic catalog entries and, like rejected proposals, no longer show the diagnostic catalog button. Approval tests support a bounded summary response; legacy full-list responses have explicit size limits. Agent service summaries include a bounded tool-name preview; enumerate `gateway_search_tools` for the full catalog. Exact calls use indexed routes and reuse schema validators.

### Observe and release active connections

The **Connections** page lists your business leases and their MCP instances; administrators can manage other users' leases. It supports filters, safe JSON details, confirmed single/batch disconnects, and five-second refresh that pauses in hidden tabs. Each lease shows current/queued calls, cumulative downstream business calls and the last dispatch time; short calls remain visible in these lease-lifetime counters. One or two instance entries use their natural height; three or more share a compact capped viewport, with scrolling for additional entries. Disconnect releases a permit and its sessions: unreferenced lazy instances drain and stop; eager instances remain, including session-isolated instances. Clients can reconnect; this does not revoke credentials. Settings provide independent instance idle reclamation and business lease validity (default 43,200 seconds; 0 disables lease expiry). The Bridge connects clients to MMG through stdio. Its heartbeats default to 90 seconds, with disconnection after three missed intervals even when lease expiry is disabled. See [Connection management](wiki/guides/manage-connections.md).

### Agent proposals with human approval

The recommended migration path is not to rebuild every MCP by hand. Give a trusted administrator token permission to submit MCP proposals, then ask an agent to:

1. Read existing Codex, Claude, generic JSON, or DSH/Cordis MCP configuration.
2. Normalize and submit multiple MCPs to MMG in one batch, up to 100 proposals.
3. Track proposal status.
4. Let a human inspect configuration, choose isolation and lifecycle policy, run a real test, and approve.
5. Use approved MCPs immediately through the gateway, without restarting connected agents.
6. Remove old client-side MCP entries only after successful validation and explicit user approval.

The agent prepares the migration; the human keeps the final decision. A proposal cannot choose privileged lifecycle or isolation fields and cannot bypass review. Read the full [MCP migration guide](wiki/guides/migrate-mcps.md) and the agent contract in [AGENTS.md](AGENTS.md).

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

On first start, MMG creates its configuration, SQLite database, and runtime data under:

- Windows: `%USERPROFILE%\.mcp-manager`
- Linux and macOS: `~/.mcp-manager`

To use another port:

~~~console
mmg serve --port 8766
~~~

For source and container options, see [Installation](wiki/getting-started/installation.md) and [Quick start](wiki/getting-started/quick-start.md).

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

The bridge only connects the client to MMG. Downstream MCP processes still belong to the gateway and follow its lifecycle rules. See [Connect clients](wiki/guides/connect-clients.md).

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
| `gateway_list_resources` | Optionally list resources, prompts, and templates |
| `gateway_read_resource` | Optionally read an exact resource or prompt URI; prompts accept string `arguments` |
| `gateway_mcp_proposals` | Optionally submit or inspect proposals with an authorized administrator token |

Reliable agents do not guess tool names or arguments: search for the MCP, retrieve the tool schema, and call the exact returned name. Discovery and pagination do not wake lazy services. See the [Gateway tool reference](wiki/reference/gateway-tools.md). Failed services report their state without advertising executable tools; keyword search supports bounded one-character typo recovery.

## Web console languages

MMG supports optional browser-side interface translation:

1. An administrator opens **System Settings → Translation Settings**.
2. Enable **Global Web Translation**, choose the source language, default target language, and translation service, then save.
3. A language control appears in the top-right corner, initially selecting the configured default when this browser has no saved choice.
4. Choose the actual target language there; the current page is translated immediately.

The language selected from the top-right control is saved in the current browser profile and takes priority over the system default. On later visits and sign-ins, MMG automatically applies that saved selection while translation remains enabled. The default target language only initializes browsers that have no local choice, so different browsers can choose independently.

Browser translation can send visible page text to the configured translation provider. Use a reviewed private provider for sensitive deployments, or keep translation disabled. Configuration, cache controls, and provider details are covered in [Interface translation and update checks](wiki/guides/interface-translation-and-updates.md).

## Administration overview

- stdio, Streamable HTTP, legacy SSE, and REST-to-MCP transports;
- newly created, imported, approved and copied services automatically attempt to publish their paged capability catalogs;
- generic JSON, Codex TOML, Claude, and DSH Cordis/registry import; exports use common `mcpServers` connection fields with a versioned MMG `config` extension for lossless re-import ([format](wiki/reference/catalog-api.md#service-configuration-export));
- console export of all service configurations as redacted JSON after confirmation; cross-page selection and batch actions in service, approval, token, log, and user lists ([console guide](wiki/guides/manage-services.md));
- cached tools, resources, prompts, and resource-template counts in service lists and accessible profile services, with service descriptions;
- persistent profile credential editors for user-isolated Bearer, Basic, API Key, and private OAuth services, for administrators and regular users; authenticated HTTP users can refresh only their own capability cache from the credential or OAuth dialog. Administrator service lists show the administrator’s own capabilities and configured-user totals for enabled users with service access ([guide](wiki/guides/manage-services.md#个人凭据配置与修改));
- independent user, token, anonymous-scope, and MCP assignment controls;
- `lazy`, `eager`, and `disabled` lifecycle modes;
- shared, per-user, and per-session runtime isolation;
- OAuth isolation for personal credentials and runtime instances;
- call logs, audit logs, background jobs, caches, and failure diagnostics;
- light, dark, and system themes;
- optional translation with provider and ignore-rule controls;
- optional PyPI update detection, with automatic checks enabled by default and manual checks always available;
- keyword and fuzzy tool search, with optional OpenAI-compatible embeddings;
- SQLite by default and optional MySQL;
- independent threshold-based startup compaction and manual space reclamation for file-backed SQLite databases, without expiring valid data ([storage maintenance](wiki/database-deployment.md#数据库空间治理)).

See [Manage services](wiki/guides/manage-services.md), [Runtime and lifecycle](wiki/concepts/runtime-and-lifecycle.md), and [Security model](wiki/security/security-model.md).

## Configuration and deployment

Environment variables override the user-home `.env`. MMG does not automatically load `.env` from the source tree or current working directory. Start from [.env.example](.env.example).

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

Read [Database deployment, migration, and backup](wiki/database-deployment.md), [Configuration](wiki/operations/configuration.md), [Deployment and upgrades](wiki/operations/deployment-and-upgrades.md), and [Backup and migration](wiki/operations/backup-and-migration.md).

## Upgrades

Upgrade with the mechanism that installed MMG:

~~~console
# uv tool
uv tool upgrade mcp-manager-gateway

# pip virtual environment
pip install --upgrade mcp-manager-gateway
~~~

Source and container installations should be updated through Git plus uv or by rebuilding/pulling the relevant image. `mmg upgrade` performs database schema migration; it does not update the installed Python package.

A one-time startup background pass reclaims historical cache residue. During operation, cache reconciliation follows MCP deletion, successful capability changes, or embedding model changes; there is no hourly cache sweep, vector TTL, or entry-count eviction. Unchanged tools reuse existing same-scope vectors without requesting the model again; disabled services and failed discovery scopes are preserved. Legacy `jobs/jobs.jsonl` is no longer read or imported and is not automatically deleted. See [Backup and migration](wiki/operations/backup-and-migration.md#缓存与历史残留).

Update detection in **System Settings → About** only reports availability. It never modifies the installation. Automatic checks can be disabled while the manual **Check for Updates** action remains available.

## Development

~~~console
uv sync --frozen --group dev
uv run pytest tests -q
node --test tests/frontend/core.test.mjs
uv build
~~~

Contributor guidance is in [Contributing](wiki/development/contributing.md); transport extension details are in [Transport plugins](wiki/development/transport-plugins.md).

## Documentation map

- [Documentation Wiki](wiki/README.md): the public documentation index
- [Installation](wiki/getting-started/installation.md) and [Quick start](wiki/getting-started/quick-start.md)
- [Client connections](wiki/guides/connect-clients.md) and [MCP migration](wiki/guides/migrate-mcps.md)
- [Progressive discovery](wiki/concepts/progressive-discovery.md) and [Runtime lifecycle](wiki/concepts/runtime-and-lifecycle.md)
- [Database deployment](wiki/database-deployment.md), [Configuration](wiki/operations/configuration.md), [Deployment](wiki/operations/deployment-and-upgrades.md), and [Troubleshooting](wiki/troubleshooting/common-issues.md)
- [CLI reference](wiki/reference/cli.md) and [Gateway tool reference](wiki/reference/gateway-tools.md)
- [Agent instructions](AGENTS.md)
- [简体中文 README](README.zh-CN.md)

## Security

MCP configuration can start local processes, access networks, and read data. Treat service administration as privileged. Review proposed commands and permissions, use least-privilege accounts and tokens, isolate different trust domains, and verify arguments before invoking tools with side effects.

An `outcome_unknown` result means a request may already have reached its destination. Agents must not automatically replay it.

## License

Copyright 2026 eraycc. Licensed under the [Apache License 2.0](LICENSE).

Large catalogs support progressive `gateway_search_tools` with `names_only: true` (no Schema reads). Query exact names again without that flag before calling. Follow `next_cursor` without changing query arguments. Native tool/resource/prompt/template listings and `gateway_list_resources` paginate; complete MCP JSON-RPC envelopes are budgeted at 1 MiB including text and structured duplication. An individually oversized complete contract returns `catalog_entry_too_large`, never a truncated Schema.
