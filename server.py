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
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal

import uvicorn
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from mcp.shared.exceptions import McpError
from pydantic import Field
from ruamel.yaml.comments import CommentedMap

import schema as schema_mod
from vault import Vault, VaultError, join_frontmatter

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("miradian")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    """os.getenv, but treat a defined-but-empty variable as absent.

    Compose and Portainer happily pass through a variable set to nothing, and
    os.getenv then returns "" rather than the default -- which is how this server
    first went live seeding an OAuth client whose id was the empty string.
    """
    return (os.getenv(name) or "").strip() or default


VAULT_PATH = _env("VAULT_PATH", "/vault")
BASE_URL = _env("MCP_BASE_URL", "http://localhost:8000")
CLIENT_ID = _env("MCP_CLIENT_ID", "miradian")
CLIENT_SECRET = _env("MCP_CLIENT_SECRET")

# Allowlist of redirect URIs for the seeded client, comma-separated. Without it
# origo accepts ANY redirect_uri: anyone who learns the client id can start an
# authorize flow and have the code delivered to a URI they control.
REDIRECT_URIS = [u.strip() for u in _env("MCP_REDIRECT_URIS").split(",") if u.strip()]
AUTO_APPROVE = _env("MCP_AUTO_APPROVE", "true").lower() == "true"
PUBLIC_REGISTRATION = _env("MCP_PUBLIC_REGISTRATION", "false").lower() == "true"
NO_AUTH = _env("MCP_NO_AUTH", "false").lower() == "true"
MCP_PATH = _env("MCP_PATH", "/mcp")
SSE_PATH = _env("SSE_PATH", "/sse")
TOKEN_TTL = int(_env("MCP_TOKEN_TTL", "3600"))
REFRESH_TOKEN_TTL = int(_env("MCP_REFRESH_TOKEN_TTL", str(30 * 24 * 3600)))
PORT = int(_env("PORT", "8000"))

# Stamp agent-written notes so they are greppable and distinguishable from your
# own. On by default -- knowing which notes a model wrote is worth more than a
# tidy frontmatter block -- but opt out if it fights your vault's conventions.
MARK_AI_GENERATED = _env("MARK_AI_GENERATED", "true").lower() != "false"
AI_GENERATED_FIELD = _env("AI_GENERATED_FIELD", "ai_generated")
STAMP_DATE = _env("STAMP_DATE", "true").lower() != "false"

# strict: refuse writes that break the schema. warn: write, but report.
# off: no schema at all. A vault with no schema note degrades to off on its own,
# so miradian still works on a vault that has never heard of any of this.
SCHEMA_MODE = _env("SCHEMA_MODE", "strict").lower()

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


def _schema():
    """The vault's schema, or None if it has no schema note (or mode is off).

    Re-read per call rather than cached: the schema lives in the vault, so editing
    it in Obsidian -- or registering a tag -- must take effect immediately.
    """
    if SCHEMA_MODE == "off":
        return None
    try:
        return schema_mod.load(vault)
    except Exception:
        return None


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
# Schema tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_get_schema(response_format: Format = "markdown") -> str:
    """The frontmatter schema this vault enforces: fields, allowed values, folders.

    Call this before writing a note. Writes that break the schema are refused, and
    the schema is the vault's own, not a fixed one — it can differ per vault.
    """
    sch = _schema()
    if not sch:
        return (
            "This vault has no frontmatter schema, so writes are not validated. "
            "Frontmatter is still read and searchable."
        )

    payload = {
        "auto_injected": sch.auto,
        "required": sch.required,
        "optional": sch.optional,
        "enums": sch.enums,
        "open_fields": sch.open,
        "constraints": sch.constraints,
        "folders": sch.folders,
        "registered_tags": len(sch.tags),
    }

    md = ["## Frontmatter schema\n"]
    if sch.auto:
        md.append(
            "**Injected by the server — do not supply these:** "
            + ", ".join(f"`{k}` ({v})" for k, v in sch.auto.items())
        )
    md.append("**Required:** " + ", ".join(f"`{k}`" for k in sch.required))
    if sch.optional:
        md.append("**Optional:** " + ", ".join(f"`{k}`" for k in sch.optional))
    md.append("")
    for fname, allowed in sch.enums.items():
        md.append(f"**`{fname}`** must be one of: {', '.join(f'`{a}`' for a in allowed)}")
    for fname, rules in sch.constraints.items():
        if (rules or {}).get("max_length"):
            md.append(f"**`{fname}`** max length: {rules['max_length']} characters")
    if sch.tags:
        md.append(
            f"\n**`tags`** must already be registered ({len(sch.tags)} exist). "
            "Browse with `vault_list_tags`; register a genuinely new one with `vault_create_tag`."
        )
    if sch.folders:
        md.append("\n**Folder conventions:**\n")
        md += [f"- `{folder}` → `{vt}`" for folder, vt in sch.folders.items()]
    md.append("\n_Values in the controlled fields are lowercased automatically._")

    return _render(payload, response_format, "\n".join(md))


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def vault_list_tags(
    similar_to: Annotated[str | None, Field(description="Rank tags by similarity to this string. Use it to find the existing tag you meant before inventing a new one.")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    response_format: Format = "markdown",
) -> str:
    """List the vault's registered tags with how often each is used.

    Always check here before introducing a new tag: near-duplicates
    (`machine-learning` vs `machinelearning`) split the index and make both
    useless for finding anything.
    """
    sch = _schema()
    counts = vault.tag_counts()          # cached in the index, not recomputed here
    registry = sch.tags if sch else sorted(counts)

    if similar_to:
        ranked = schema_mod.difflib.get_close_matches(similar_to.lower(), registry, n=limit, cutoff=0.3)
        items = [{"tag": t, "uses": counts.get(t, 0)} for t in ranked]
        meta = {"total": len(items), "count": len(items), "offset": 0, "has_more": False, "next_offset": None}
        if not items:
            return (
                f"No registered tag resembles {similar_to!r}. If it is genuinely new, "
                f"call vault_create_tag({similar_to.lower()!r}) to register it."
            )
    else:
        ordered = sorted(registry, key=lambda t: (-counts.get(t, 0), t))
        window, meta = _page(ordered, limit, offset)
        items = [{"tag": t, "uses": counts.get(t, 0)} for t in window]

    md = f"**{meta['total']} registered tags**\n\n" + "\n".join(
        f"- `{i['tag']}` — {i['uses']} use(s)" for i in items
    )
    return _render({"tags": items, **meta}, response_format, md + _pagination_footer(meta))


@dataclass
class SchemaChangeApproval:
    """What the human is asked to confirm before the vocabulary is widened."""

    approve: bool


async def _require_approval(ctx: Context, action: str) -> str | None:
    """Ask the human to approve a schema change. Returns an error string if not approved.

    Widening the vocabulary is the one thing an agent must not do on its own: every
    new tag, folder or enum value is permanent, and a vocabulary that grows by
    accident is exactly the drift the schema exists to prevent.

    This uses MCP elicitation (spec 2025-06-18), which fails closed. A client with
    no human attached -- an autonomous agent, a cron run -- cannot support
    elicitation, so the request errors and the change is refused. That is the
    desired outcome, and it is enforced here rather than trusted to the client:
    tool annotations like destructiveHint are hints, not a boundary.

    The response_type is explicit rather than None: an empty elicitation schema is
    ambiguous under the spec, and some clients render it as a form with no controls
    -- an approval prompt nobody can approve.
    """
    try:
        result = await ctx.elicit(
            f"miradian wants to change the vault schema:\n\n{action}\n\n"
            "This permanently widens the vault's controlled vocabulary. Approve?",
            response_type=SchemaChangeApproval,
        )
    except (McpError, ToolError) as e:
        # The client has no human to ask. Refuse -- the whole point of the gate.
        logger.info("schema change refused, no elicitation support: %s", e)
        return (
            f"Error: {action} requires human approval, and this client cannot ask for it "
            "(no elicitation support). Schema changes are refused for unattended agents. "
            "Ask a human to make this change, or reuse an existing value."
        )
    except Exception:
        # A bug here (a bad response_type, say) must not masquerade as "the human
        # said no" -- that would silently disable the gate rather than fail loudly.
        logger.exception("elicitation failed unexpectedly")
        raise

    if isinstance(result, AcceptedElicitation):
        if getattr(result.data, "approve", False):
            return None
        return "Declined: the human did not approve. Reuse an existing value instead."
    if isinstance(result, DeclinedElicitation):
        return "Declined: the human refused this change. Reuse an existing value instead."
    return "Cancelled: no approval was given, so nothing was changed."


def _append_to_schema(key: str, value, dedupe: bool = True) -> str | None:
    """Add a value to a list inside the schema note. Returns an error, or None."""
    try:
        note = vault.get(schema_mod.SCHEMA_NOTE)
        block = note.frontmatter.get("schema")
        current = block.setdefault(key, [])
        current.append(value)
        if dedupe:
            # Sorted so the registry's diffs stay readable in git.
            block[key] = sorted({str(v).strip() for v in current})
        vault.write_atomic(note.rel, join_frontmatter(note.frontmatter, note.body))
    except (VaultError, OSError, AttributeError) as e:
        return f"Error: could not update the schema ({e})."
    return None


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def vault_create_tag(
    ctx: Context,
    tag: Annotated[str, Field(description="The new tag, lowercase kebab-case, e.g. 'machine-learning'.")],
    force: Annotated[bool, Field(description="Register even though a similar tag already exists. Only after checking vault_list_tags and concluding the existing one means something different.")] = False,
) -> str:
    """Register a genuinely new tag. Requires human approval.

    Deliberately separate from writing a note: a tag that appears because of a typo
    is indistinguishable from one that appears on purpose, and the first kind
    quietly destroys the index. Check vault_list_tags first — reuse beats invention
    nearly every time.
    """
    sch = _schema()
    if not sch:
        return "Error: this vault has no schema note, so there is no tag registry to add to."

    tag = tag.strip().lower()

    problem = schema_mod.suggest_tag_name(tag)
    if problem:
        return f"Error: {problem}"

    if sch.known_tag(tag):
        return f"`{tag}` is already registered. Use it directly."

    close = sch.close_tags(tag)
    if close and not force:
        return (
            f"Error: {tag!r} closely resembles existing tag(s): {', '.join(repr(c) for c in close)}.\n"
            "Reuse one of those if it means the same thing — near-duplicate tags split the index "
            "and make both halves useless.\n"
            f"If {tag!r} genuinely means something different, call vault_create_tag({tag!r}, force=true)."
        )

    refused = await _require_approval(ctx, f"register a new tag: {tag!r}")
    if refused:
        return refused

    if err := _append_to_schema("tags", tag):
        return err
    return f"Registered `{tag}`. It can now be used in note frontmatter."


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def vault_create_folder(
    ctx: Context,
    folder: Annotated[str, Field(description="Vault-relative folder, e.g. '06 🔬 Research'.")],
    vault_type: Annotated[str, Field(description="The vault_type notes in this folder carry. Must be an allowed vault_type, or register it first with vault_update_schema.")],
) -> str:
    """Create a folder and record what vault_type it holds. Requires human approval.

    The folder→vault_type mapping is what lets a bad vault_type be corrected from
    the path ("you are writing to '03 🛠 Projects', which holds vault_type:
    project"), so a folder without one is a folder the schema cannot help with.
    """
    sch = _schema()
    if not sch:
        return "Error: this vault has no schema note."

    vault_type = vault_type.strip().lower()
    allowed = sch.enums.get("vault_type", [])
    if allowed and vault_type not in allowed:
        return (
            f"Error: vault_type {vault_type!r} is not allowed. Allowed: {', '.join(allowed)}. "
            "Add it first with vault_update_schema, then create the folder."
        )

    try:
        target = vault.resolve_path(folder.rstrip("/") + "/.keep")
    except VaultError as e:
        return f"Error: {e}"

    if target.parent.is_dir() and folder in sch.folders:
        return f"`{folder}` already exists and is mapped to vault_type: {sch.folders[folder]!r}."

    refused = await _require_approval(
        ctx, f"create folder {folder!r}, holding vault_type: {vault_type!r}"
    )
    if refused:
        return refused

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        note = vault.get(schema_mod.SCHEMA_NOTE)
        block = note.frontmatter.get("schema")
        block.setdefault("folders", {})[folder] = vault_type
        vault.write_atomic(note.rel, join_frontmatter(note.frontmatter, note.body))
    except (VaultError, OSError, AttributeError) as e:
        return f"Error: could not create the folder ({e})."

    # Git does not track empty directories, and Obsidian shows them fine, so the
    # folder only becomes real once a note lands in it.
    return (
        f"Created `{folder}`, mapped to vault_type: `{vault_type}`. "
        "It is empty — write a note into it to make it stick."
    )


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
async def vault_update_schema(
    ctx: Context,
    field: Annotated[str, Field(description="The controlled field to extend, e.g. 'vault_type' or 'status'.")],
    value: Annotated[str, Field(description="The new allowed value to add.")],
) -> str:
    """Add an allowed value to a controlled vocabulary. Requires human approval.

    Use sparingly. The whole worth of `status` and `vault_type` is that they are
    short, closed lists — a vocabulary that grows whenever something doesn't fit is
    just a free-text field with extra steps.
    """
    sch = _schema()
    if not sch:
        return "Error: this vault has no schema note."

    field, value = field.strip(), value.strip().lower()

    if field not in sch.enums:
        return (
            f"Error: {field!r} is not a controlled vocabulary. "
            f"Controlled fields: {', '.join(sch.enums)}."
        )
    if value in sch.enums[field]:
        return f"`{value}` is already an allowed {field}."

    refused = await _require_approval(
        ctx,
        f"add {value!r} to the allowed values for {field!r} "
        f"(currently: {', '.join(sch.enums[field])})",
    )
    if refused:
        return refused

    try:
        note = vault.get(schema_mod.SCHEMA_NOTE)
        block = note.frontmatter.get("schema")
        block["enums"][field].append(value)
        vault.write_atomic(note.rel, join_frontmatter(note.frontmatter, note.body))
    except (VaultError, OSError, AttributeError, KeyError) as e:
        return f"Error: could not update the schema ({e})."

    return f"`{value}` is now an allowed `{field}`."


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

    # Server-injected, never taken from the caller: a model should not be able to
    # backdate a note or claim it wrote nothing.
    out.pop("date", None)
    if STAMP_DATE:
        out["date"] = date.today().isoformat()
    if MARK_AI_GENERATED:
        out[AI_GENERATED_FIELD] = True

    sch = _schema()
    if sch:
        schema_mod.normalize(out, sch)
        errors = schema_mod.validate(out, path, sch)
        if errors and SCHEMA_MODE == "strict":
            return "Error: frontmatter does not match the vault schema.\n\n" + "\n".join(
                f"- {e}" for e in errors
            )
        if errors:
            warning = "\n\nWarning: frontmatter does not match the vault schema:\n" + "\n".join(
                f"- {e}" for e in errors
            )
        else:
            warning = ""
    else:
        warning = ""

    try:
        rel = vault.write_atomic(path, join_frontmatter(out, content))
    except VaultError as e:
        return f"Error: {e}"
    except OSError as e:
        return f"Error: could not write `{path}` ({e})."

    return f"{'Overwrote' if exists else 'Created'} `{rel}`." + warning


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
    try:
        note = vault.get(path)
    except VaultError as e:
        return f"Error: {e}"

    full_text = join_frontmatter(note.frontmatter, note.body)
    count = full_text.count(old_string)

    if count == 0:
        return f"Error: text not found in `{note.rel}`. Read the note with vault_read_note and match the text exactly, including whitespace."
    if count > 1 and not replace_all:
        return (
            f"Error: text appears {count} times in `{note.rel}`. Include more surrounding "
            "context to make it unique, or pass replace_all=true."
        )

    updated = full_text.replace(old_string, new_string) if replace_all else full_text.replace(old_string, new_string, 1)
    try:
        vault.write_atomic(note.rel, updated)
    except (VaultError, OSError) as e:
        return f"Error: could not write `{note.rel}` ({e})."

    return f"Edited `{note.rel}` ({count if replace_all else 1} replacement(s))."


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
def vault_update_frontmatter(
    path: Annotated[str, Field(description="Note path or bare name.")],
    set_fields: Annotated[dict[str, Any] | None, Field(description="Fields to add or overwrite, e.g. {'status': 'done'}.")] = None,
    unset_fields: Annotated[list[str] | None, Field(description="Field names to remove.")] = None,
) -> str:
    """Add, change, or remove frontmatter fields, leaving the body and key order intact."""
    try:
        note = vault.get(path)
    except VaultError as e:
        return f"Error: {e}"

    if not set_fields and not unset_fields:
        return "Error: nothing to do — supply set_fields, unset_fields, or both."

    fm = note.frontmatter
    for k, v in (set_fields or {}).items():
        fm[k] = v
    missing = [k for k in (unset_fields or []) if k not in fm]
    for k in unset_fields or []:
        fm.pop(k, None)

    # Same gate as vault_write_note. Without it this tool is a way round the
    # schema: set status to anything, or unset a required field.
    sch = _schema()
    if sch:
        schema_mod.normalize(fm, sch)
        errors = schema_mod.validate(fm, note.rel, sch)
        if errors and SCHEMA_MODE == "strict":
            return "Error: that change would break the vault schema.\n\n" + "\n".join(
                f"- {e}" for e in errors
            )

    try:
        vault.write_atomic(note.rel, join_frontmatter(fm, note.body))
    except (VaultError, OSError) as e:
        return f"Error: could not write `{note.rel}` ({e})."

    msg = f"Updated frontmatter on `{note.rel}`."
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
    from origo import OAuthMiddleware, OAuthProvider

    if not CLIENT_SECRET:
        raise SystemExit("MCP_CLIENT_SECRET is required (or set MCP_NO_AUTH=true for local testing).")

    auth = OAuthProvider(
        base_url=BASE_URL,
        clients={CLIENT_ID: CLIENT_SECRET},
        client_redirect_uris={CLIENT_ID: REDIRECT_URIS} if REDIRECT_URIS else None,
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

            if not NO_AUTH:
                logger.info("auth: client_id=%r base_url=%s", CLIENT_ID, BASE_URL)
                if not REDIRECT_URIS:
                    logger.warning(
                        "auth: no MCP_REDIRECT_URIS set — any redirect_uri will be accepted. "
                        "Anyone who learns the client id can have an auth code delivered to a "
                        "URI they control. Set MCP_REDIRECT_URIS to the URIs your client uses."
                    )
                if BASE_URL.startswith("http://localhost"):
                    logger.warning(
                        "auth: MCP_BASE_URL is still the default (%s). It must be the URL clients "
                        "actually reach this server at, or their tokens bind to the wrong resource "
                        "and every call 401s.",
                        BASE_URL,
                    )

            sch = _schema()
            logger.info(
                "schema: %s",
                f"{SCHEMA_MODE}, {len(sch.tags)} tags, enums={list(sch.enums)}"
                if sch
                else "none found in vault — writes are not validated",
            )
            yield


app.router.lifespan_context = _lifespan


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
