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

## Design notes

- **Writes are atomic** (temp file + `os.replace`). The vault is typically a
  Syncthing replica; a non-atomic write racing another machine's Obsidian
  produces `sync-conflict-*` files.
- **Frontmatter round-trips** through `ruamel.yaml`, preserving key order and
  formatting. Agent writes set `ai_generated: true`.
- **Never touches git.** Writes land as ordinary files; review and commit them
  yourself. Signing stays with you, not the agent.
- **Ignored:** `.obsidian/`, `.stversions/`, `.stfolder/`, `.git/`, `.trash/`,
  and `*sync-conflict*`. Excalidraw notes stay in the link graph but their
  bodies (embedded blobs) are excluded from content search.
- **Path containment:** absolute paths and `..` traversal are rejected.
- The index is in-memory and rebuilt lazily from mtimes. A 250-note vault indexes
  in ~200 ms; there is no database to keep in sync.
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

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `VAULT_PATH` | `/vault` | Vault mount inside the container |
| `MCP_BASE_URL` | — | Externally visible base URL; OAuth metadata is derived from it |
| `MCP_CLIENT_ID` | `miradian` | |
| `MCP_CLIENT_SECRET` | — | **Required** unless `MCP_NO_AUTH=true` |
| `MCP_AUTO_APPROVE` | `true` | Skip the consent page |
| `MCP_PUBLIC_REGISTRATION` | `false` | Keep off: private server |
| `MCP_TOKEN_TTL` | `3600` | Access token lifetime |
| `MCP_REFRESH_TOKEN_TTL` | `2592000` | Refresh token lifetime |
| `MCP_NO_AUTH` | `false` | Local testing only — **never** in deployment |
| `MCP_PATH` / `SSE_PATH` | `/mcp` / `/sse` | |

## Deploy

```bash
docker compose up -d --build
```

`compose.yml` publishes on loopback and the WireGuard address only, never
`0.0.0.0`, so the port is unreachable from the LAN or WAN regardless of router
configuration. Secrets come from the Portainer stack's environment-variable UI.

The container runs as the vault directory's owning uid, so writes carry the uid
the host's Syncthing expects.

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q

# run against a copy of a real vault, no auth
VAULT_PATH=/path/to/vault-copy MCP_NO_AUTH=true PORT=8000 .venv/bin/python server.py
```

Point it at a **copy** first. It has write tools.
