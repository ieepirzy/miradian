# Structured-document service for Miradian

Status: proposed, first slice implemented
Date: 2026-09-17
Consumer: MiraDesign (project-state backend)

## 1. Why

Miradian today is an MCP server whose only surface is agent-facing `vault_*` tools
over an Obsidian vault. MiraDesign needs to use the same vault as a **project-state
store**: enumerate projects, read and write structured Markdown documents, patch
metadata without touching prose, and find out what changed since it last looked —
over an ordinary HTTP/JSON API, not through agent-oriented tool wrappers.

The constraint that shapes everything below: **Obsidian semantics terminate inside
Miradian.** MiraDesign consumes Markdown + structured metadata and nothing else.

## 2. Current architecture

Three flat modules, no package:

| Module | Role |
|---|---|
| `vault.py` | `Vault`: mtime-driven in-memory index of every `.md` in the vault, wikilink graph, `split_frontmatter`/`join_frontmatter` (ruamel round-trip), `resolve_path` containment, `write_atomic` (temp + `os.replace`), `trash` |
| `schema.py` | Loads a frontmatter schema **from the vault** (`METADATA.md`), validates and normalises |
| `server.py` | 14 `vault_*` MCP tools, config from env, origo OAuth, `/mcp` + `/sse` from one Starlette app |

Facts established by reading the code and the live vault:

- The index is rebuilt lazily from mtime+size on every public `Vault` call; the link
  graph is only recomputed when a note actually changed. ~7 ms at 250 notes.
- Writes are atomic because the vault is a Syncthing replica.
- Path containment rejects absolute paths and `..`; `.obsidian/`, `.stversions/`,
  `.stfolder/`, `.git/`, `.trash/` and `*sync-conflict*` are never indexed.
- `Vault.get()` resolves **bare names Obsidian-style** (by stem, shallowest wins).
  That is Obsidian semantics and must not leak into the generic API.
- The real vault's `METADATA.md` requires `title, tags, status, vault_type, summary`
  on every write, with `status` and `vault_type` as closed enums and `tags` a closed
  registry. Widening any of them is gated behind **MCP elicitation**.
- origo (the auth provider) supports only `authorization_code` and `refresh_token`.
  There is **no `client_credentials` grant**, and its middleware treats every path
  outside a small exact-match public set as protected.
- The `03 🛠 Projects` root contains both project **directories** (`Loimi`, `Mira`,
  `Muutto365`, …) and loose single-file notes (`i2pd-hardening.md`).
- `09 Tasks` is empty. There is no existing project-management convention to
  preserve; MiraDesign will define it.

## 3. Gaps

1. **No service layer.** Every operation is an MCP tool returning a display string.
   Nothing returns structured data to a non-MCP caller.
2. **No HTTP API**, and no auth mechanism a headless service can use.
3. **No project concept.** The vault is a flat note namespace.
4. **No change detection.** A consumer can only re-list everything.
5. **No move/rename.** `Vault` can write and trash, not rename.
6. **No stable version identity.** `Note` carries `mtime`/`size`; nothing a consumer
   can use for optimistic concurrency.
7. **Vault frontmatter policy is entangled with write mechanics** in `server.py`.

## 4. Proposed boundary

```
                MCP agents                      MiraDesign
                    │                                │
            server.py  (vault_* tools)          api.py  (/api/v1, token auth)
            + vault schema policy                    │
            + AI stamping                            │
            + elicitation gates                      │
                    └───────────┬────────────────────┘
                                │
                         documents.py            changes.py
                 DocumentService: projects,      poll cursor,
                 list/read/write/patch/move      JSON journal
                                │
                             vault.py
                 index · containment · atomic write ·
                 frontmatter round-trip · wikilink graph
                                │
                           Markdown on disk
```

- `vault.py` keeps the filesystem and Obsidian knowledge. It gains a content `hash`
  and a `move()` operation; nothing else changes.
- `documents.py` is the generic layer. It knows about *projects*, *structured
  documents*, *metadata* and *versions*. It knows nothing about wikilinks, the vault
  schema, Obsidian, or MiraDesign.
- `server.py` and `api.py` are two presentations of `documents.py`. Neither reaches
  past it into the filesystem.

### 4.1 Decision: the vault schema does not apply to the API surface

`METADATA.md` requires `title, tags, status, vault_type, summary` with closed
vocabularies, and widening them needs MCP elicitation — which has no HTTP analogue.
A MiraDesign task document (`doc_type: task`, `status: todo`) fails on four counts,
so enforcing the vault schema on `/api/v1` would make the API unable to write a
single document.

Therefore: **the frontmatter schema is a vault-surface policy, enforced on the
`vault_*` MCP tools only.** The structured-document API validates *structure*
(is it Markdown with parseable frontmatter?) and not *vocabulary*. This is
consistent with the schema living in the vault as a personal-knowledge convention,
and with "Markdown + structured metadata is the abstraction".

Consequences, stated so they are not a surprise:
- Documents written through `/api/v1` will not satisfy `METADATA.md` and will show
  up as schema-invalid if later edited through an MCP tool in `strict` mode.
- MiraDesign owns the `doc_type` vocabulary. **Miradian never interprets or
  validates `doc_type` values.**
- `date` injection and `ai_generated` stamping stay on the MCP write path. The only
  field the generic layer writes on its own is `uid` (§4.4).

### 4.2 What is a project

A **project is a directory** directly under a configured project root.

- `PROJECT_ROOTS` (env, `::`-separated vault-relative paths), default `03 🛠 Projects`.
- A directory under a root is a project whether or not it holds any documents, so
  *starting a project is `mkdir`* — exactly the requirement.
- Loose `.md` files sitting at root level are **not** projects and are not listed as
  project documents. They are ordinary notes that happen to live there.
- Nested directories inside a project are folders within that project, not projects.
- Project `id` is the root-relative directory path; `name` is the directory name.

The default is set explicitly rather than derived from `METADATA.md.folders`, so the
generic layer never reads the vault schema.

### 4.3 What is a structured document

Every `.md` under a project is a *document*. A document is **structured** when its
frontmatter contains the marker field `DOC_TYPE_FIELD` (default `doc_type`).

- `list_documents` returns structured documents only, unless
  `include_unstructured=true`.
- This is the mechanism that keeps normal project knowledge and project-management
  state in the same tree without either colonising the other: a design note with no
  `doc_type` is invisible to MiraDesign's default listing but still readable by path.
- The marker is a *presence* test. The value is opaque to Miradian.
- Carrying the marker is what opts a file into being *managed*: it is listed by
  default, it appears in the change feed, and it gets a `uid` stamped (§4.4).
  Removing the marker opts it back out.

### 4.4 Identity and versioning

**`uid` is identity. `path` is location.** They are separate fields and a consumer
must never use one for the other.

| Field | Meaning |
|---|---|
| `id` | The document's `uid`: 32 hex characters, assigned once, immutable. Survives moves, renames and re-foldering. |
| `path` | Vault-relative POSIX location. Mutable — it changes whenever the human drags the file in Obsidian. |
| `version` | `sha256(file bytes)[:16]`. Changes whenever the file changes. |
| `mtime` | ISO-8601 UTC. Informational — not an identity. |

The uid lives in the document's own frontmatter (`UID_FIELD`, default `uid`), so it
travels with the file through Syncthing, git and Obsidian, and survives Miradian
losing all of its own state. Nothing external has to be kept in sync.

**Miradian assigns uids, and only to managed documents.** A document is *managed*
exactly when its frontmatter carries the `doc_type` marker (§4.3). On the first scan
that sees a managed document without a uid, Miradian stamps one — an atomic
frontmatter-only write that leaves the body untouched.

This is a real, deliberate side effect of reading: a poll can write to the vault.
It is bounded and it is opt-in:

- Only documents that already declare `doc_type` are ever touched. An ordinary note
  is never modified, so the knowledge vault is not colonised by the PM system.
- It happens once per document, ever. Steady-state polling writes nothing.
- It is atomic and frontmatter-only, so an Obsidian window open on the file sees a
  metadata change, not a content conflict.
- `ASSIGN_UID=false` turns it off; documents without a uid are then reported with
  `id: null` and are addressable by path only.

**Duplicate uids** happen — a human duplicates a note in Obsidian and the copy
carries the original's uid. On scan, the path the journal already associates with
that uid keeps it; every other claimant is re-stamped with a fresh uid. Deterministic
and self-repairing, with a warning logged.

Writes, patches, moves and deletes accept `expected_version`. A mismatch is a
`409 conflict` carrying the current version, so a polling consumer gets
last-writer-detected rather than last-writer-wins.

### 4.5 Change detection

A JSON journal outside the vault, rebuilt by diffing scans. No database, no watcher,
no event bus.

The feed covers **managed documents only** — those carrying the `doc_type` marker.
A note that gains the marker appears as `created`; one that loses it appears as
`deleted`. Unstructured notes never enter the feed, which is also why they never
need an identity.

State (`$STATE_DIR/changes.json`, atomic write):

```json
{
  "epoch": "<uuid4, new whenever state is lost or rebuilt>",
  "seq": 412,
  "docs": {"<uid>": {"path": "...", "version": "...", "seq": 410, "doc_type": "task"}},
  "tombstones": [{"id": "<uid>", "path": "...", "seq": 411}]
}
```

- `scan()` runs **lazily at the start of every poll**. It refreshes the `Vault`
  index (mtime-driven, cheap) and diffs the current managed-document set against
  `docs`. Each changed document takes the next `seq`. No background thread, nothing
  to supervise.
- Change types: `created`, `updated`, `moved`, `deleted`. Because identity is the
  uid, a move is detected exactly — same uid, different path — with no content-hash
  guessing. A document that both moved and changed reports `moved` and carries the
  new version.
- Cursor is the opaque string `"<epoch>:<seq>"`.
- `poll(cursor)` returns changes with `seq > cursor_seq`, ordered, plus the new
  cursor.
- **Recovery:** an unknown or mismatched `epoch` (state file lost, corrupt, or from
  a previous deployment) returns `reset: true` and the full current document set. A
  consumer's only recovery obligation is "on `reset`, drop your state and re-ingest".
- Tombstones are capped (`CHANGE_TOMBSTONE_LIMIT`, default 5000, oldest dropped).
- Documented limitation: a document created *and* deleted between two polls is never
  reported. That is the accepted cost of poll-based eventual consistency.

`STATE_DIR` must not be inside the vault — Syncthing would replicate the journal and
git would track it. This is asserted at startup, not left to configuration care.

### 4.6 Transport and auth

origo has no `client_credentials` grant, so a headless MiraDesign cannot obtain an
OAuth token without a browser dance. Its middleware also protects every path with an
exact-match public list, which cannot express `/api/v1/*`.

So the JSON API is **not** behind origo. Instead:

```python
oauth_protected = OAuthMiddleware(mcp_app, provider=auth)   # unchanged behaviour

async def root(scope, receive, send):
    if scope["type"] == "http" and scope["path"].startswith("/api/"):
        await api_app(scope, receive, send)      # static-token auth
    else:
        await oauth_protected(scope, receive, send)
```

`lifespan` scopes always go to the MCP app, or FastMCP's session managers never
start. A ~20-line dispatcher, no Mount path rewriting, and the working OAuth stack
is untouched.

API auth is a static bearer token: `MIRADIAN_API_TOKENS=name:secret,name2:secret2`,
compared with `hmac.compare_digest`. **With no tokens configured the API refuses
every request** (unless `MCP_NO_AUTH=true`, which is local testing only) — it fails
closed rather than shipping an open vault endpoint by omission.

An origo access token is also accepted on `/api/v1`, so a human or agent that already
holds one does not need a second credential.

### 4.7 Write scope

The service layer can read and write **any** vault-relative path, because the MCP
tools need that. The **HTTP API refuses paths outside the configured project roots**.
MiraDesign gets the project tree; it does not get the personal knowledge base.

## 5. API

RPC-flavoured over HTTP: reads are `GET` with query parameters, mutations are `POST`
with a JSON body. Deliberately *not* `/documents/{id}` — ids are path-shaped, and a
percent-encoded `/` in a path segment is normalised or rejected inconsistently by
ASGI servers and proxies.

```
GET  /api/v1/health
GET  /api/v1/projects
GET  /api/v1/folders?project=
GET  /api/v1/documents?project=&doc_type=&include_unstructured=&limit=&offset=
GET  /api/v1/documents/get?id=          (or ?path= for documents with no uid)
POST /api/v1/documents/create           {path, body, metadata, expected_version}
POST /api/v1/documents/write            {id|path, body, metadata, expected_version}
POST /api/v1/documents/patch-metadata   {id|path, set, unset, expected_version}
POST /api/v1/documents/move             {id|path, to, expected_version}
POST /api/v1/documents/delete           {id|path, expected_version}
GET  /api/v1/changes?cursor=&limit=
```

Every mutation accepts `id` (uid — the normal case) or `path` (for a document that
has no uid yet). `create` takes a path because the document does not exist yet; the
response carries the uid Miradian assigned.

Full reference in `docs/api.md`.

## 6. MCP compatibility

- All 14 `vault_*` tools keep their names, signatures and behaviour. Existing agents
  are unaffected.
- Shared mechanics, separate policy. The pieces both surfaces genuinely have in
  common already live below them — `split_frontmatter`/`join_frontmatter`,
  `write_atomic`, `resolve_path` in `vault.py`, and now `documents.apply_metadata`,
  which `vault_update_frontmatter` and the API's `patch_metadata` both call so
  set/unset means one thing in this server.
- What is *not* shared is policy, and deliberately: `vault_write_note` injects
  `date`, stamps `ai_generated` and enforces `METADATA.md`. Re-pointing it at the
  unvalidated service would silently drop all three. `patch_metadata` takes an
  optional `policy` callback for exactly this reason — the mechanism is one
  function, the rules are the caller's.
- Two new read-only MCP tools expose the project view to agents over the same
  service: `project_list` and `project_documents`. They are thin; they add no logic.

## 7. Migration and backward compatibility

- Additive only. No existing tool, env var, route or on-disk format changes meaning.
- New env vars all have working defaults except `MIRADIAN_API_TOKENS`; with it unset
  the API is present but refuses everything, which is the safe default for an
  existing deployment that upgrades without knowing about it.
- `compose.yml` gains a named volume for `STATE_DIR`. Without it the journal is lost
  on every restart, which is *correct but wasteful*: consumers get `reset: true` and
  re-ingest.
- A vault with no `PROJECT_ROOTS` match yields zero projects and an empty change
  feed. Nothing errors.

## 8. Unresolved decisions

See `design/unresolved.md`.
