"""miradian — MCP server exposing CRUD + link-graph access to an Obsidian vault.

Serves both /mcp (streamable HTTP) and /sse. Auth is origo in-process: private
mode, seeded client, auto-approve. The server never touches git -- writes flow
out via Syncthing and are reviewed and signed on the workstation.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Literal

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from ruamel.yaml.comments import CommentedMap

from vault import Note, Vault, VaultError, join_frontmatter

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("miradian")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VAULT_PATH = os.getenv("VAULT_PATH", "/vault")
BASE_URL = os.getenv("MCP_BASE_URL", "http://localhost:8000")
CLIENT_ID = os.getenv("MCP_CLIENT_ID", "miradian")
CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET", "")
AUTO_APPROVE = os.getenv("MCP_AUTO_APPROVE", "true").lower() == "true"
PUBLIC_REGISTRATION = os.getenv("MCP_PUBLIC_REGISTRATION", "false").lower() == "true"
NO_AUTH = os.getenv("MCP_NO_AUTH", "false").lower() == "true"
# Opts the seeded client out of exact redirect_uri matching at /authorize (origo's
# ANY_REDIRECT_URI sentinel). For connector surfaces with undocumented/churning
# callback URLs. MCP_CLIENT_SECRET still gates /token either way — see origo's
# README ("Redirect URIs for pre-registered clients") for the full trade-off.
ALLOW_ANY_REDIRECT_URI = os.getenv("MCP_ANY_REDIRECT_URI", "false").lower() == "true"
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
SSE_PATH = os.getenv("SSE_PATH", "/sse")
TOKEN_TTL = int(os.getenv("MCP_TOKEN_TTL", "3600"))
REFRESH_TOKEN_TTL = int(os.getenv("MCP_REFRESH_TOKEN_TTL", str(30 * 24 * 3600)))
PORT = int(os.getenv("PORT", "8000"))

# Stamp agent-written notes so they are greppable and distinguishable from your
# own. On by default -- knowing which notes a model wrote is worth more than a
# tidy frontmatter block -- but opt out if it fights your vault's conventions.
MARK_AI_GENERATED = os.getenv("MARK_AI_GENERATED", "true").lower() != "false"
AI_GENERATED_FIELD = os.getenv("AI_GENERATED_FIELD", "ai_generated")
STAMP_DATE = os.getenv("STAMP_DATE", "true").lower() != "false"

DEFAULT_LIMIT = 25

vault = Vault(VAULT_PATH)
mcp = FastMCP("miradian")

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

Format = Literal["markdown", "json"]


def _page(items: list, limit: int, offset: int) -> tuple[list, dict]:
    total = len(items)
    offset = max(0, offset)
    window = items[offset : offset + limit]
    end = offset + len(window)
    return window, {
        "total": total,
        "count": len(window),
        "offset": offset,
        "has_more": end < total,
        "next_offset": end if end < total else None,
    }


def _render(payload: dict, fmt: Format, markdown: str) -> str:
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False) if fmt == "json" else markdown


def _pagination_footer(meta: dict) -> str:
    if not meta["has_more"]:
        return f"\n_{meta['count']} of {meta['total']}._"
    return (
        f"\n_{meta['count']} of {meta['total']} — more available. "
        f"Call again with offset={meta['next_offset']}._"
    )


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_list_notes(
    folder: Annotated[str | None, Field(description="Folder to list, e.g. '03 🛠 Projects'. Omit for the whole vault.")] = None,
    glob: Annotated[str | None, Field(description="Glob against the note path, e.g. '*.md' or 'Logbook/*'.")] = None,
    limit: Annotated[int, Field(description="Max notes to return.", ge=1, le=200)] = DEFAULT_LIMIT,
    offset: Annotated[int, Field(description="Notes to skip, for paging.", ge=0)] = 0,
    response_format: Format = "markdown",
) -> str:
    """List notes in the vault, optionally filtered by folder or glob.

    Returns note paths with their title and tags. Use vault_search to find notes
    by content instead.
    """
    try:
        notes = vault.notes()
    except VaultError as e:
        return f"Error: {e}"

    rels = sorted(notes)
    if folder:
        prefix = folder.strip("/") + "/"
        rels = [r for r in rels if r.startswith(prefix)]
    if glob:
        from pathlib import PurePosixPath

        rels = [r for r in rels if PurePosixPath(r).match(glob)]

    if not rels:
        return f"No notes matched (folder={folder!r}, glob={glob!r}). Use vault_list_notes with no filter to see the vault root."

    window, meta = _page(rels, limit, offset)
    items = [
        {
            "path": r,
            "title": notes[r].frontmatter.get("title", notes[r].stem),
            "tags": notes[r].tags,
        }
        for r in window
    ]

    md = "\n".join(
        f"- `{i['path']}`" + (f" — {i['title']}" if i["title"] else "") + (f"  [{', '.join(i['tags'])}]" if i["tags"] else "")
        for i in items
    )
    return _render({"notes": items, **meta}, response_format, md + _pagination_footer(meta))


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_read_note(
    path: Annotated[str, Field(description="Note path ('02 📚 Knowledge/Physics/x.md') or bare name ('x'), which is resolved like an Obsidian link.")],
    response_format: Format = "markdown",
) -> str:
    """Read a note's frontmatter and full body.

    Accepts either a vault path or a bare note name.
    """
    try:
        note = vault.get(path)
    except VaultError as e:
        return f"Error: {e}"

    payload = {
        "path": note.rel,
        "frontmatter": dict(note.frontmatter),
        "body": note.body,
        "tags": note.tags,
        "links": [l.target for l in note.links],
    }
    md = f"# `{note.rel}`\n\n{join_frontmatter(note.frontmatter, note.body)}"
    return _render(payload, response_format, md)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_search(
    query: Annotated[str | None, Field(description="Text to find in note bodies and frontmatter. Omit to filter purely on metadata.")] = None,
    regex: Annotated[bool, Field(description="Treat query as a regular expression.")] = False,
    path_glob: Annotated[str | None, Field(description="Restrict to paths matching this glob, e.g. '04 🧠 Logbook/*'.")] = None,
    tag: Annotated[str | None, Field(description="Only notes carrying this frontmatter tag.")] = None,
    frontmatter_filter: Annotated[dict[str, Any] | None, Field(description="Frontmatter fields that must match, e.g. {'status': 'active'}.")] = None,
    context_lines: Annotated[int, Field(description="Lines of context around each hit.", ge=0, le=10)] = 1,
    case_sensitive: bool = False,
    limit: Annotated[int, Field(description="Max notes to return.", ge=1, le=100)] = DEFAULT_LIMIT,
    offset: Annotated[int, Field(description="Notes to skip, for paging.", ge=0)] = 0,
    response_format: Format = "markdown",
) -> str:
    """Grep the vault: search note content and frontmatter, with metadata filters.

    Filters compose — all supplied ones must match. Excalidraw drawing bodies are
    excluded from content search (they are embedded binary blobs), though their
    frontmatter is still searched.
    """
    try:
        hits = vault.search(
            query,
            regex=regex,
            path_glob=path_glob,
            tag=tag,
            frontmatter=frontmatter_filter,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:  # a bad user regex should be fixable, not a crash
        return f"Error: invalid search ({e}). If regex=True, check the pattern syntax."

    if not hits:
        return (
            f"No notes matched (query={query!r}, tag={tag!r}, path_glob={path_glob!r}). "
            "Try a broader query, drop a filter, or use vault_list_notes to browse."
        )

    window, meta = _page(hits, limit, offset)

    blocks = []
    for h in window:
        head = f"### `{h['path']}`"
        if h.get("frontmatter_match"):
            head += "  _(frontmatter match)_"
        lines = [f"- **L{m['line']}**: {m['text']}" for m in h["matches"][:5]]
        extra = len(h["matches"]) - 5
        if extra > 0:
            lines.append(f"- _…{extra} more hits in this note._")
        blocks.append(head + ("\n" + "\n".join(lines) if lines else ""))

    return _render({"results": window, **meta}, response_format, "\n\n".join(blocks) + _pagination_footer(meta))


# ---------------------------------------------------------------------------
# Graph tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_get_backlinks(
    path: Annotated[str, Field(description="Note path or bare name.")],
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    response_format: Format = "markdown",
) -> str:
    """What links to this note. The inbound half of the vault's link graph."""
    try:
        note = vault.get(path)
        links = vault.backlinks(note.rel)
    except VaultError as e:
        return f"Error: {e}"

    if not links:
        return f"No notes link to `{note.rel}`."

    window, meta = _page(links, limit, offset)
    md = f"**{meta['total']} notes link to `{note.rel}`:**\n\n" + "\n".join(f"- `{r}`" for r in window)
    return _render({"note": note.rel, "backlinks": window, **meta}, response_format, md + _pagination_footer(meta))


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_get_links(
    path: Annotated[str, Field(description="Note path or bare name.")],
    response_format: Format = "markdown",
) -> str:
    """Outgoing links from a note, flagging any that resolve to no existing note.

    Unresolved links are notes referenced but never written — effectively a to-do
    list embedded in the vault.
    """
    try:
        note = vault.get(path)
        pairs = vault.outgoing(note.rel)
    except VaultError as e:
        return f"Error: {e}"

    resolved = [{"target": l.target, "path": r, "embed": l.embed, "line": l.line} for l, r in pairs if r]
    missing = sorted({l.target for l, r in pairs if not r})

    if not pairs:
        return f"`{note.rel}` has no outgoing links."

    md = f"**Outgoing links from `{note.rel}`:**\n\n"
    md += "\n".join(f"- `{r['path']}`" + (" _(embed)_" if r["embed"] else "") for r in resolved) or "- _none resolved_"
    if missing:
        md += "\n\n**Unresolved (referenced but not written):**\n\n" + "\n".join(f"- [[{t}]]" for t in missing)

    return _render({"note": note.rel, "links": resolved, "unresolved": missing}, response_format, md)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_get_neighborhood(
    path: Annotated[str, Field(description="Note path or bare name.")],
    depth: Annotated[int, Field(description="Hops to traverse, following links in both directions.", ge=1, le=3)] = 1,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    response_format: Format = "markdown",
) -> str:
    """The local subgraph around a note: everything within N hops, in or out.

    Use this to pull the cluster of context surrounding a topic.
    """
    try:
        note = vault.get(path)
        hood = vault.neighborhood(note.rel, depth)
    except VaultError as e:
        return f"Error: {e}"

    if not hood:
        return f"`{note.rel}` is isolated — no links in or out."

    items = sorted(hood.items(), key=lambda kv: (kv[1], kv[0]))
    window, meta = _page(items, limit, offset)
    md = f"**{meta['total']} notes within {depth} hop(s) of `{note.rel}`:**\n\n" + "\n".join(
        f"- `{r}` _(distance {d})_" for r, d in window
    )
    payload = {"note": note.rel, "depth": depth, "neighbors": [{"path": r, "distance": d} for r, d in window], **meta}
    return _render(payload, response_format, md + _pagination_footer(meta))


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


# destructiveHint is True because mode="overwrite" replaces an existing note
# wholesale. A client using annotations to decide what to auto-approve must not
# wave that through.
@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False})
def vault_write_note(
    path: Annotated[str, Field(description="Vault-relative path ending in .md, e.g. '00 📥 Inbox/idea.md'.")],
    content: Annotated[str, Field(description="Markdown body, without frontmatter.")],
    frontmatter: Annotated[dict[str, Any] | None, Field(description="Frontmatter fields. The vault schema is: title, date, tags, vault_type, status, summary, source.")] = None,
    mode: Annotated[Literal["create", "overwrite"], Field(description="'create' refuses to clobber an existing note.")] = "create",
) -> str:
    """Create or overwrite a note.

    Stamps the note as AI-written and dates it, unless the server is configured
    otherwise. Use vault_edit_note for small changes to an existing note rather
    than rewriting it wholesale.
    """
    try:
        full = vault.resolve_path(path)
    except VaultError as e:
        return f"Error: {e}"

    exists = full.exists()
    if exists and mode == "create":
        return (
            f"Error: `{path}` already exists. Pass mode='overwrite' to replace it, "
            "or use vault_edit_note to change part of it."
        )

    out = CommentedMap()
    for k, v in (frontmatter or {}).items():
        out[k] = v
    if STAMP_DATE:
        out.setdefault("date", date.today().isoformat())
    if MARK_AI_GENERATED:
        out[AI_GENERATED_FIELD] = True

    try:
        rel = vault.write_atomic(path, join_frontmatter(out, content))
    except VaultError as e:
        return f"Error: {e}"
    except OSError as e:
        return f"Error: could not write `{path}` ({e})."

    return f"{'Overwrote' if exists else 'Created'} `{rel}`."


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
def vault_edit_note(
    path: Annotated[str, Field(description="Note path or bare name.")],
    old_string: Annotated[str, Field(description="Exact text to replace. Must appear exactly once unless replace_all is set.")],
    new_string: Annotated[str, Field(description="Replacement text.")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence instead of requiring a unique match.")] = False,
) -> str:
    """Replace exact text inside a note, leaving the rest of the file untouched.

    Preferred over vault_write_note for edits: it cannot accidentally drop content
    the agent did not read.
    """
    outcome: dict[str, int] = {}

    def _transform(note: Note) -> str:
        full_text = join_frontmatter(note.frontmatter, note.body)
        count = full_text.count(old_string)

        if count == 0:
            raise VaultError(
                f"text not found in `{note.rel}`. Read the note with vault_read_note and match the text exactly, including whitespace."
            )
        if count > 1 and not replace_all:
            raise VaultError(
                f"text appears {count} times in `{note.rel}`. Include more surrounding "
                "context to make it unique, or pass replace_all=true."
            )

        outcome["count"] = count
        return full_text.replace(old_string, new_string) if replace_all else full_text.replace(old_string, new_string, 1)

    # The read (vault.get), the replace, and the write happen under one lock
    # acquisition (see Vault.edit) so a concurrent edit of the same note can't
    # sneak in between the read and the write and have its change silently
    # overwritten by this call's stale-based write.
    try:
        rel = vault.edit(path, _transform)
    except VaultError as e:
        return f"Error: {e}"
    except OSError as e:
        return f"Error: could not write `{path}` ({e})."

    count = outcome["count"]
    return f"Edited `{rel}` ({count if replace_all else 1} replacement(s))."


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def vault_update_frontmatter(
    path: Annotated[str, Field(description="Note path or bare name.")],
    set_fields: Annotated[dict[str, Any] | None, Field(description="Fields to add or overwrite, e.g. {'status': 'done'}.")] = None,
    unset_fields: Annotated[list[str] | None, Field(description="Field names to remove.")] = None,
) -> str:
    """Add, change, or remove frontmatter fields, leaving the body and key order intact."""
    if not set_fields and not unset_fields:
        return "Error: nothing to do — supply set_fields, unset_fields, or both."

    missing: list[str] = []

    def _transform(note: Note) -> str:
        fm = note.frontmatter
        for k, v in (set_fields or {}).items():
            fm[k] = v
        missing.extend(k for k in (unset_fields or []) if k not in fm)
        for k in unset_fields or []:
            fm.pop(k, None)
        return join_frontmatter(fm, note.body)

    # See vault_edit_note: the read, the frontmatter mutation, and the write
    # happen under one lock acquisition (Vault.edit) so a concurrent edit of the
    # same note can't land in the gap and be silently overwritten.
    try:
        rel = vault.edit(path, _transform)
    except VaultError as e:
        return f"Error: {e}"
    except OSError as e:
        return f"Error: could not write `{path}` ({e})."

    msg = f"Updated frontmatter on `{rel}`."
    if missing:
        msg += f" (Not present, so not removed: {', '.join(missing)}.)"
    return msg


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False})
def vault_delete_note(
    path: Annotated[str, Field(description="Note path or bare name.")],
) -> str:
    """Move a note to the vault's .trash/ folder.

    Not a hard delete: the note leaves the vault (and the deletion syncs to other
    machines) but a recovery copy stays in .trash/ on the server.
    """
    try:
        note = vault.get(path)
        dest = vault.trash(note.rel)
    except VaultError as e:
        return f"Error: {e}"
    except OSError as e:
        return f"Error: could not delete `{path}` ({e})."

    return f"Moved `{note.rel}` to `{dest}`. Recoverable from .trash/ on the server."


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

app = mcp.http_app(path=MCP_PATH, transport="http", stateless_http=True)
sse_app = mcp.http_app(path=SSE_PATH, transport="sse")

# Serve both transports from one app so a single origo provider protects both.
# /mcp is primary; /sse exists because some connector surfaces still require it.
for route in sse_app.routes:
    app.router.routes.append(route)

if not NO_AUTH:
    from origo import ANY_REDIRECT_URI, OAuthMiddleware, OAuthProvider

    if not CLIENT_SECRET:
        raise SystemExit("MCP_CLIENT_SECRET is required (or set MCP_NO_AUTH=true for local testing).")

    auth = OAuthProvider(
        base_url=BASE_URL,
        clients={CLIENT_ID: CLIENT_SECRET},
        client_redirect_uris={CLIENT_ID: ANY_REDIRECT_URI} if ALLOW_ANY_REDIRECT_URI else None,
        auto_approve=AUTO_APPROVE,
        public_registration=PUBLIC_REGISTRATION,
        token_ttl=TOKEN_TTL,
        refresh_token_ttl=REFRESH_TOKEN_TTL,
        mcp_path=MCP_PATH,
    )

    # Adopt origo's own routes and state wholesale instead of hand-listing them.
    # Its endpoints read 11 app.state attributes and the set grows between
    # versions (0.1.9 added allow_private_cimd); a hand-copied subset compiles
    # fine and then 500s at /authorize. Sourcing both from the provider's app
    # means a future origo can add state without silently breaking us. This also
    # picks up /userinfo and /.well-known/openid-configuration for free.
    oauth_app = auth.asgi_app()
    for r in reversed(oauth_app.routes):
        app.router.routes.insert(0, r)

    app.add_middleware(OAuthMiddleware, provider=auth)

    for _key, _value in vars(oauth_app.state)["_state"].items():
        setattr(app.state, _key, _value)

# Both transports carry their own lifespan (session managers); run both.
_http_lifespan = app.router.lifespan_context
_sse_lifespan = sse_app.router.lifespan_context


@asynccontextmanager
async def _lifespan(scope):
    async with _http_lifespan(scope):
        async with _sse_lifespan(scope):
            vault.refresh()
            logger.info("miradian: indexed %d notes from %s", len(vault.notes()), VAULT_PATH)
            yield


app.router.lifespan_context = _lifespan


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
