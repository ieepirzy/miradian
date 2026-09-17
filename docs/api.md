# Miradian structured-document API (v1)

A generic API over Markdown documents with structured frontmatter. The consumer is
whatever wants to use a directory of Markdown files as project state — MiraDesign is
the first, but nothing here is shaped around it.

**There are no Obsidian semantics in this API.** Wikilinks, the vault's frontmatter
schema, the tag registry, `.obsidian/` and bare-name link resolution all exist in
Miradian and none of them reach this surface. Two consequences worth stating: paths
are matched exactly (a bare note name does not resolve to a file), and writes are
**not** validated against the vault's `METADATA.md` schema, so a service consumer
never hits a human-approval prompt.

Base URL: `<MCP_BASE_URL>/api/v1`

## Concepts

| Term | Meaning |
|---|---|
| **project** | A directory directly under a configured project root. Creating one is `mkdir`; nothing else is required. |
| **document** | A `.md` file under a project root. |
| **structured / managed document** | A document whose frontmatter carries the marker field (`doc_type` by default). Only these are project state. |
| **`id`** | The document's `uid`: 32 hex characters, assigned once, **immutable**. Survives moves and renames. Opaque — do not parse it. |
| **`path`** | Where the document currently lives, vault-relative. **Mutable.** It changes whenever the human moves the file. |
| **`version`** | Content hash. Changes whenever the file changes. Hand it back on write to get conflict detection. |

`id` is identity and `path` is location. Store the `id` on your durable rows; treat
the `path` as display data you refresh from the change feed.

### Structured vs. ordinary documents

A project folder holds both project-management state and ordinary knowledge — design
notes, research, meeting scribbles. The marker field is what separates them:

```yaml
---
doc_type: work_item     # ← this file is project state
title: Wire the API
---
```

A document with no `doc_type` is listed only when you ask for it
(`include_unstructured=true`), never appears in the change feed, and is never
modified by Miradian. **Miradian does not interpret or validate `doc_type` values** —
the vocabulary is yours.

### Identity is stamped into the file

The first time Miradian sees a managed document without a `uid`, it writes one into
the frontmatter. This is an atomic, frontmatter-only write; the body is untouched.

- Only documents that already declare `doc_type` are ever touched.
- It happens once per document. Steady-state polling writes nothing.
- The uid lives in the file, so it survives Syncthing, git, and Miradian losing all
  of its own state.
- A human who deletes the `uid:` line by hand destroys that identity: the document
  reads as deleted and recreated, with a new id.
- `ASSIGN_UID=false` disables it; documents then report `id: null` and are
  addressable by `path` only.

## Auth

```
Authorization: Bearer <token>
```

Tokens are configured on the server as `MIRADIAN_API_TOKENS=name:secret,...`. An
access token issued by the server's OAuth provider is accepted too.

With no tokens configured the API answers `503 not_configured` to everything except
`/health` — it fails closed rather than exposing a vault by omission.

`GET /api/v1/health` needs no token.

## Scope

Every path this API touches must be inside a configured project root. Anything else
returns `403 out_of_scope`. The rest of the vault is not reachable from here.

## Endpoints

Reads are `GET` with query parameters; mutations are `POST` with a JSON body.
Identifiers never appear in the URL path — they are path-shaped, and percent-encoded
slashes inside a path segment are handled inconsistently by ASGI servers and proxies.

### `GET /api/v1`

Capabilities and server configuration. Read this first and degrade on absence rather
than assuming a feature exists.

```json
{
  "service": "miradian",
  "api_version": "v1",
  "capabilities": {
    "documents": true, "projects": true, "atomic_write": true,
    "optimistic_concurrency": true, "change_feed": true, "stable_ids": true,
    "links": false, "search": false, "schema": false
  },
  "project_roots": ["03 🛠 Projects"],
  "doc_type_field": "doc_type",
  "uid_field": "uid"
}
```

`links` and `search` are `false`: Miradian has both internally, but they are
Obsidian-shaped and are not exposed generically yet.

### `GET /api/v1/health`

`{"status": "ok"}`. Unauthenticated, for container healthchecks.

### `GET /api/v1/projects`

```json
{"projects": [
  {"id": "03 🛠 Projects/Loimi", "name": "Loimi", "path": "03 🛠 Projects/Loimi",
   "root": "03 🛠 Projects", "document_count": 4, "updated_at": "2026-09-17T18:31:02+00:00"}
]}
```

`document_count` counts managed documents only, so a project full of ordinary notes
reports `0`. An empty project reports `updated_at: null`.

### `GET /api/v1/folders?project=`

`{"folders": ["03 🛠 Projects/Loimi/specs"]}` — subdirectories, for building paths.

### `GET /api/v1/documents`

| Parameter | Default | Meaning |
|---|---|---|
| `project` | all | Restrict to one project id |
| `doc_type` | all | Exact match, case-insensitive |
| `include_unstructured` | `false` | Also return documents with no marker field |
| `path_prefix` | — | Restrict to a subtree |
| `limit` / `offset` | `100` / `0` | Paging; max `limit` is 1000 |

```json
{
  "documents": [{
    "id": "f2eb76e1aad74130ba82487848a45e43",
    "path": "03 🛠 Projects/Loimi/plan.md",
    "project": "03 🛠 Projects/Loimi",
    "doc_type": "objective",
    "title": "Ship v1",
    "metadata": {"doc_type": "objective", "title": "Ship v1", "uid": "f2eb…"},
    "version": "49db7b7c56f59f4e",
    "mtime": "2026-09-17T18:31:02.733565+00:00",
    "structured": true
  }],
  "total": 1, "count": 1, "offset": 0, "has_more": false, "next_offset": null
}
```

`metadata` is the document's whole frontmatter, converted to JSON-safe values (YAML
dates become ISO strings).

### `GET /api/v1/documents/get?id=` *(or `?path=`)*

The same object plus `body` — the Markdown after the frontmatter.

### `POST /api/v1/documents/create`

```json
{"path": "03 🛠 Projects/Loimi/task-9.md", "body": "Do it.",
 "metadata": {"doc_type": "work_item", "title": "Nine"}}
```

`201` with the created document, including the uid assigned in the same round trip.
`409 already_exists` if the path is taken.

### `POST /api/v1/documents/write`

```json
{"id": "f2eb…", "body": "New prose.", "metadata": {...}, "expected_version": "49db…"}
```

Replaces the body. **Omitting `metadata` leaves the frontmatter untouched** — a
consumer that only edits prose cannot destroy metadata it never read. Supplying
`metadata` replaces the block wholesale, except the uid, which is preserved.

### `POST /api/v1/documents/patch-metadata`

```json
{"id": "f2eb…", "set": {"status": "done"}, "unset": ["owner"], "expected_version": "49db…"}
```

Changes individual frontmatter fields. The body, the key order, YAML comments and
quoting style are all preserved. Returns the document plus `not_present`: the names
you asked to unset that were not there.

`uid` cannot be unset.

### `POST /api/v1/documents/move`

```json
{"id": "f2eb…", "to": "03 🛠 Projects/Muutto365/plan.md", "expected_version": "49db…"}
```

The `id` does not change; the `path` does. Refuses to overwrite an existing file.

### `POST /api/v1/documents/delete`

```json
{"id": "f2eb…", "expected_version": "49db…"}
```

→ `{"id": "…", "path": "…", "deleted": true, "recoverable_at": ".trash/…"}`

### `GET /api/v1/changes?cursor=&limit=`

```json
{
  "changes": [{
    "type": "moved",
    "seq": 7,
    "id": "9413bc6c6caa4adc853e0d0a5a6bab8c",
    "path": "03 🛠 Projects/Loimi/back-again.md",
    "previous_path": "03 🛠 Projects/Empty/relocated.md",
    "project": "03 🛠 Projects/Loimi",
    "doc_type": "work_item",
    "title": "Task 1",
    "version": "404bec788f56c186",
    "mtime": "2026-09-17T18:33:10+00:00"
  }],
  "cursor": "7f1fa2b5751a4fb0a2a9cbc249729514:7",
  "reset": false,
  "has_more": false
}
```

Types: `created`, `updated`, `moved`, `deleted`.

## Consuming the change feed

1. Poll `GET /api/v1/changes` with your stored cursor (omit it the first time).
2. If `reset` is `true`, **discard your view and re-ingest what comes back.** That is
   the only recovery you have to implement. It happens on your first poll, after the
   server's journal is lost or rebuilt, and if your cursor is unrecognisable.
3. Apply the changes as **upserts keyed by `id`** — not as literal create/update/delete
   commands. A batch that follows a `reset` (`has_more` was true, so you polled again
   with the new cursor) carries real types like `updated` and `moved` for documents
   you have not ingested yet, so treat an `updated` for an unknown id as a create.
4. Store the returned `cursor`.
5. If `has_more` is `true`, poll again immediately with the new cursor.

Things the feed guarantees, and things it does not:

- **One entry per document, not per edit.** Five edits between two polls arrive as
  one `updated`; you re-read the document anyway. This is a change feed, not an
  audit log — the vault's git history is the audit log.
- **Moves are exact.** Identity is the uid, so a move is never guessed from content
  and is never reported as delete+create.
- **Eventual consistency.** Changes are detected when you poll, not when they happen.
  Lag is bounded by your polling interval.
- **A document created and deleted between two of your polls is never reported.**
  That is the accepted cost of poll-based detection.
- **Tombstones are capped** (5000 by default). A consumer away long enough to fall
  off the end gets `reset` instead, which is the right answer anyway.
- Documents that are not managed never appear. A note that *gains* `doc_type` appears
  as `created`; one that loses it appears as `deleted`.

## Errors

```json
{"error": "version_conflict", "message": "…", "current_version": "404bec788f56c186"}
```

| Status | `error` | When |
|---|---|---|
| 400 | `bad_request` | Missing or malformed parameters, non-JSON body |
| 401 | `unauthorized` | Missing or invalid bearer token |
| 403 | `out_of_scope` | Path outside the configured project roots |
| 404 | `not_found` | No document with that id or path |
| 409 | `already_exists` | Creating over an existing path |
| 409 | `version_conflict` | `expected_version` did not match; `current_version` is included |
| 422 | `malformed_document` | The file's frontmatter does not parse, so rewriting it would corrupt it further. Fix it by hand |
| 422 | `policy_rejected` | A server-side policy refused the change (not used on this surface) |
| 503 | `not_configured` | No API credentials configured on the server |

## Concurrency

The vault is edited by a human in Obsidian, by agents over MCP, and by you, at the
same time. Every write is atomic (temp file + `rename`), so nobody ever observes a
half-written document.

To avoid clobbering someone else's edit, pass the `version` you read as
`expected_version`. A mismatch is a `409` carrying the current version rather than a
silent overwrite. Without it, writes are last-writer-wins.

## Configuration (server side)

| Variable | Default | Meaning |
|---|---|---|
| `PROJECT_ROOTS` | `03 🛠 Projects` | `::`-separated vault-relative roots. `::`, not `,`, because folder names contain commas and spaces |
| `DOC_TYPE_FIELD` | `doc_type` | The marker field that makes a document managed |
| `UID_FIELD` | `uid` | Frontmatter field holding the document's identity |
| `ASSIGN_UID` | `true` | Stamp uids into managed documents. `false` means `id: null` everywhere |
| `STATE_DIR` | `~/.local/state/miradian` (`/state` in the container) | Change journal location. Must be outside the vault; the server refuses otherwise |
| `CHANGE_TOMBSTONE_LIMIT` | `5000` | How many deletions the journal remembers |
| `MIRADIAN_API_TOKENS` | — | `name:secret,name2:secret2`. Unset means the API refuses everything |
