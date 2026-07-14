# miradian

MCP server giving AI agents CRUD and link-graph access to an Obsidian vault.

Reads the vault as **plain markdown on disk**. No Obsidian process, no database,
no plugins.

## Why not an existing Obsidian MCP?

Every widely-used one — `mcp-obsidian`, `obsidian-mcp-server`, and the `/mcp/`
endpoint now built into the [Local REST API plugin](https://github.com/coddingtonbear/obsidian-local-rest-api)
— talks to a **running Obsidian desktop app**. There is no headless mode. On an
always-on headless server that means running Electron under a virtual display
(KasmVNC), where a modal dialog can wedge the GUI and take the MCP down with it.

All that buys you is Obsidian's `metadataCache`, Dataview, and command execution.
If your vault doesn't lean on those, reading the files directly is simpler and
strictly more robust. miradian parses the markdown itself.

## Tools

All tools are prefixed `vault_` so they don't collide with other MCP servers
sharing an agent's context. Every list/search tool paginates (`limit`/`offset`,
returning `has_more`/`next_offset`), and all take `response_format=markdown|json`.

**CRUD**
| Tool | Purpose |
|---|---|
| `vault_list_notes` | Browse notes by folder or glob |
| `vault_read_note` | Read frontmatter + body |
| `vault_write_note` | Create or overwrite a note |
| `vault_edit_note` | Replace exact text in place |
| `vault_update_frontmatter` | Set/unset frontmatter fields |
| `vault_delete_note` | Move a note to `.trash/` |

**Search**
| Tool | Purpose |
|---|---|
| `vault_search` | Grep content *and* frontmatter, with tag / frontmatter / path filters |

**Schema**
| Tool | Purpose |
|---|---|
| `vault_get_schema` | The frontmatter schema this vault enforces |
| `vault_list_tags` | Registered tags with usage counts; `similar_to` finds the one you meant |
| `vault_create_tag` | Register a genuinely new tag — **needs human approval** |
| `vault_create_folder` | Create a folder and map it to a vault_type — **needs human approval** |
| `vault_update_schema` | Add an allowed value to a controlled vocabulary — **needs human approval** |

**Graph**
| Tool | Purpose |
|---|---|
| `vault_get_backlinks` | What links to this note |
| `vault_get_links` | Outgoing links, flagging unresolved ones |
| `vault_get_neighborhood` | Local subgraph within N hops |

`vault_get_links` reports links pointing at notes that don't exist yet — in
practice, a to-do list of notes you meant to write.

Link parsing covers `[[Note]]`, `[[Note|alias]]`, `[[Note#heading]]`,
`[[Note^block]]`, `![[embed]]`, wikilinks inside frontmatter, and frontmatter
`aliases`. Resolution matches Obsidian's: by name, not path, preferring the
shallowest match when a name is ambiguous.

**Markdown only.** Images, PDFs, and other binaries are ignored entirely.

## Frontmatter as an index

Frontmatter is only useful if you can query it — find every `active` project, or
everything tagged `ai`. That breaks the moment the vocabulary drifts: `wip` vs
`in progress` vs `In Progress` are three rows in the index and each is invisible
to a search for the others.

So miradian enforces a schema **defined by the vault itself**, in `METADATA.md`:

- **Closed vocabularies.** `vault_type` and `status` are fixed lists. A write
  using anything else is refused, with the near-match named.
- **Governed tags.** `tags` is open but registered. An unregistered tag is
  refused, pointing at close existing ones; registering a new one is a deliberate
  act (`vault_create_tag`), because a tag born of a typo is indistinguishable
  from one added on purpose, and the first kind silently splits the index.
- **Case is folded** on controlled fields, so `Branding` can't become a second
  `branding`. Prose fields (`title`, `summary`, `source`) keep their case.
- **`date` and `ai_generated` are server-injected** and ignored if supplied — an
  agent cannot backdate a note or claim it didn't write one.
- **Errors tell the agent what to do next**, including the folder convention:
  writing `vault_type: porject` into `03 🛠 Projects/` gets *"did you mean
  'project'? — you are writing to '03 🛠 Projects', which holds vault_type:
  project"*.

### Widening the vocabulary needs a human

Every new tag, folder, or enum value is permanent, and a vocabulary that grows by
accident is exactly the drift the schema exists to prevent. So the three tools
that widen it are gated behind [MCP elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation):
the server asks the human to approve, and only then writes.

This **fails closed**. A client with no human attached — an autonomous agent, a
cron run — cannot support elicitation, so the request errors and the change is
refused. An unattended agent can read, search, and write notes; it cannot change
what the schema permits.

That boundary is enforced server-side, not delegated. Tool annotations like
`destructiveHint` are only *hints* — the client decides what to do with them, so
they are not a security boundary. Elicitation is.

`SCHEMA_MODE=strict|warn|off`. A vault with no `METADATA.md` schema block
degrades to `off` on its own, so miradian works fine on a vault that has never
heard of any of this.

## Design notes

- **Writes are atomic** (temp file + `os.replace`). Vaults are commonly synced
  (Syncthing, Dropbox, iCloud), and a non-atomic write racing another machine's
  Obsidian produces conflict files.
- **Frontmatter round-trips** through `ruamel.yaml`, preserving key order and
  formatting. Agent writes are stamped `ai_generated: true` so you can always
  grep for what a model wrote; set `MARK_AI_GENERATED=false` to opt out, or
  rename the key with `AI_GENERATED_FIELD`.
- **Never touches git.** Writes land as ordinary files; review and commit them
  yourself. Signing stays with you, not the agent.
- **Ignored:** `.obsidian/`, `.stversions/`, `.stfolder/`, `.git/`, `.trash/`,
  and `*sync-conflict*`. Excalidraw notes stay in the link graph but their
  bodies (embedded blobs) are excluded from content search.
- **Path containment:** absolute paths and `..` traversal are rejected.
- The index is in-memory and refreshed lazily from mtimes; there is no database
  to keep in sync. The link graph is only rebuilt when a note actually changed,
  so an unchanged vault costs a directory walk plus one stat per note. Measured
  per-tool-call overhead: ~7 ms at 250 notes, ~30 ms at 1k, ~270 ms at 10k. Very
  large vaults would want an inotify watcher instead of the walk.
- **Thread-safe.** FastMCP runs sync tools in a thread pool and agents issue tool
  calls in parallel, so the index is guarded by a reentrant lock. Without it, one
  call rebuilding the index while another reads it crashes the reader.

## Endpoints

Serves both transports from one app:

- `/mcp` — streamable HTTP (primary)
- `/sse` — SSE, for connector surfaces that still require it

Auth is [origo](https://github.com/ieepirzy/origo) in-process (OAuth 2.1 + PKCE),
running in private mode with a seeded client. A single access token works against
both endpoints. Requires **origo >= 0.1.9** for the refresh-token grant, which
lets access tokens stay short-lived (1h) with rotating refresh tokens (30d).

### `MCP_BASE_URL`

The one setting that silently breaks things when it's wrong. It is the URL clients
**reach the server at** — not the address it binds to (that's `BOUND_IP`).

origo derives everything from it: the issuer, the `/authorize` and `/token` URLs
it advertises in discovery, and the resource identifier tokens are bound to
(`MCP_BASE_URL` + `MCP_PATH`). The server never infers it from the incoming
request. Get it wrong and the client is redirected somewhere it can't reach, or
its token's resource fails to match and every call 401s.

Include the port if clients use one:

| Setup | `MCP_BASE_URL` |
|---|---|
| Behind a reverse proxy (TLS terminated there) | `https://miradian.example.com` |
| Straight over a VPN, no proxy | `http://10.8.0.8:27125` (`BOUND_IP` + `PORT`) |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `VAULT_PATH` | `/vault` | Vault mount inside the container |
| `MCP_BASE_URL` | — | The URL clients **reach** this server at (not the bind address). See below |
| `MCP_CLIENT_ID` | `miradian` | |
| `MCP_CLIENT_SECRET` | — | **Required** unless `MCP_NO_AUTH=true` |
| `MCP_AUTO_APPROVE` | `true` | Skip the consent page |
| `MCP_PUBLIC_REGISTRATION` | `false` | Keep off: private server |
| `MCP_TOKEN_TTL` | `3600` | Access token lifetime |
| `MCP_REFRESH_TOKEN_TTL` | `2592000` | Refresh token lifetime |
| `MCP_NO_AUTH` | `false` | Local testing only — **never** in deployment |
| `MARK_AI_GENERATED` | `true` | Stamp agent-written notes. Set `false` to opt out |
| `AI_GENERATED_FIELD` | `ai_generated` | Frontmatter key used for that stamp |
| `STAMP_DATE` | `true` | Add `date:` to new notes. Set `false` to opt out |
| `SCHEMA_MODE` | `strict` | `strict` refuses invalid writes, `warn` allows + reports, `off` disables |
| `MCP_PATH` / `SSE_PATH` | `/mcp` / `/sse` | |
| `BOUND_IP` | `127.0.0.1` | Host address the port is published on. Use a private/VPN interface. Never `0.0.0.0` |
| `PORT` | `27125` | Host port |

## Deploy

```bash
docker compose up -d --build
```

Copy `.env.example` to `.env` and fill it in.

`compose.yml` publishes on loopback plus `BOUND_IP`, never `0.0.0.0`. Point
`BOUND_IP` at a private or VPN interface (a WireGuard peer address, say) and the
port stays unreachable from your LAN and the internet regardless of router
configuration. It defaults to loopback.

The container runs as the vault directory's owning uid, so writes carry the uid
the host expects -- which matters if the vault is a sync replica.

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q

# run against a copy of a real vault, no auth
VAULT_PATH=/path/to/vault-copy MCP_NO_AUTH=true PORT=8000 .venv/bin/python server.py
```

Point it at a **copy** first. It has write tools.
