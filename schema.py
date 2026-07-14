"""Frontmatter schema: loaded from the vault, enforced on writes.

The schema lives in the vault (METADATA.md by default), not in this code. That
is deliberate: the vault owns its own conventions, they are editable in Obsidian
like any other note, and there is exactly one place to change them.

Frontmatter is an index -- the thing you query to find every active project or
every note tagged `ai`. An index only works if the vocabulary is closed. A free-
form `status` field drifts into `wip`/`in progress`/`In Progress` and stops being
queryable, so closed fields are rejected outright, and the open one (`tags`) is
governed: an unregistered tag is refused with near-matches, and registering a new
one is a deliberate act (vault_create_tag) rather than a side effect of a typo.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SCHEMA_NOTE = "METADATA.md"

# New tags must look like the vault's existing convention: lowercase kebab-case.
TAG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass
class Schema:
    auto: dict = field(default_factory=dict)          # server-injected, never caller-supplied
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    enums: dict[str, list[str]] = field(default_factory=dict)   # closed vocabularies
    open: list[str] = field(default_factory=list)     # free-form fields
    constraints: dict = field(default_factory=dict)
    folders: dict[str, str] = field(default_factory=dict)       # path prefix -> vault_type
    tags: list[str] = field(default_factory=list)     # the registered tag vocabulary

    def folder_hint(self, path: str) -> str | None:
        """The vault_type conventionally used in the folder this path lands in."""
        top = PurePosixPath(path).parts[0] if PurePosixPath(path).parts else ""
        return self.folders.get(top)

    def known_tag(self, tag: str) -> bool:
        """Is this tag registered? Values arrive already lowercased by normalize()."""
        return tag.strip().lower() in self.tags

    def close_tags(self, tag: str, n: int = 4) -> list[str]:
        return difflib.get_close_matches(tag.lower(), self.tags, n=n, cutoff=0.6)


def load(vault) -> Schema | None:
    """Read the schema from the vault's schema note. None if it has none."""
    try:
        note = vault.get(SCHEMA_NOTE)
    except Exception:
        return None

    raw = note.frontmatter.get("schema")
    if not isinstance(raw, dict):
        return None

    def as_list(v) -> list[str]:
        if v is None:
            return []
        return [str(x) for x in (v if isinstance(v, list) else [v])]

    return Schema(
        auto=dict(raw.get("auto") or {}),
        required=as_list(raw.get("required")),
        optional=as_list(raw.get("optional")),
        enums={k: as_list(v) for k, v in (raw.get("enums") or {}).items()},
        open=as_list(raw.get("open")),
        constraints=dict(raw.get("constraints") or {}),
        folders={str(k): str(v) for k, v in (raw.get("folders") or {}).items()},
        tags=as_list(raw.get("tags")),
    )


def _values(raw) -> list[str]:
    if raw is None:
        return []
    return [str(x).strip() for x in (raw if isinstance(raw, list) else [raw]) if str(x).strip()]


def normalize(frontmatter: dict, schema: Schema) -> dict:
    """Lowercase the controlled-vocabulary fields, in place.

    Case is the cheapest source of drift: `Branding` and `branding` are two rows
    in the index and one of them is invisible to any query for the other. Folding
    case at the tool layer means a model cannot introduce that variant at all,
    rather than being told off for it afterwards.

    Only the governed fields are folded. `title`, `summary` and `source` are prose
    or citations and keep their capitalisation.
    """
    governed = set(schema.enums) | {"tags"}
    for key in governed:
        if key not in frontmatter or frontmatter[key] is None:
            continue
        value = frontmatter[key]
        if isinstance(value, list):
            frontmatter[key] = [str(v).strip().lower() if isinstance(v, str) else v for v in value]
        elif isinstance(value, str):
            frontmatter[key] = value.strip().lower()
    return frontmatter


def validate(frontmatter: dict, path: str, schema: Schema) -> list[str]:
    """Return human-readable errors, each ending in what the agent should do next.

    Empty list means the frontmatter is valid.
    """
    errors: list[str] = []

    missing = [k for k in schema.required if not _values(frontmatter.get(k))]
    if missing:
        errors.append(
            f"Missing required frontmatter: {', '.join(missing)}. "
            "Call vault_get_schema to see every field and its allowed values."
        )

    # Closed vocabularies. These are the fields the whole index hangs off, so a
    # near-miss ("Project", "wip") is rejected rather than quietly written.
    for field_name, allowed in schema.enums.items():
        for value in _values(frontmatter.get(field_name)):
            if value in allowed:
                continue
            # Case never reaches here: normalize() folded it, and the schema's
            # vocabularies are lowercase. So a mismatch is a real one.
            msg = f"{field_name}: {value!r} is not an allowed value. Allowed: {', '.join(allowed)}."

            near = difflib.get_close_matches(value, allowed, n=1, cutoff=0.6)
            if near:
                msg += f" Did you mean {near[0]!r}?"

            # The folder the note is going into usually settles vault_type.
            if field_name == "vault_type":
                hint = schema.folder_hint(path)
                if hint:
                    msg += f" You are writing to {PurePosixPath(path).parts[0]!r}, which holds vault_type: {hint!r}."
            errors.append(msg)

    # Governed open vocabulary: tags must already be registered.
    for tag in _values(frontmatter.get("tags")):
        if schema.known_tag(tag):
            continue
        msg = f"tags: {tag!r} is not a registered tag."
        close = schema.close_tags(tag)
        if close:
            msg += f" Closest registered tags: {', '.join(repr(c) for c in close)}."
        msg += (
            f" Call vault_list_tags to browse all {len(schema.tags)} registered tags and reuse one."
            f" Only if it is genuinely new, call vault_create_tag({tag!r}) to register it, then retry."
        )
        errors.append(msg)

    for field_name, rules in schema.constraints.items():
        value = frontmatter.get(field_name)
        max_len = (rules or {}).get("max_length")
        if max_len and value is not None:
            text = " ".join(_values(value))
            if len(text) > int(max_len):
                errors.append(
                    f"{field_name} is {len(text)} characters; the maximum is {max_len}. "
                    f"It is an index field, not prose -- write a 1-2 line summary."
                )

    return errors


def suggest_tag_name(tag: str) -> str | None:
    """Why a proposed tag name is rejected, or None if it is acceptable."""
    t = tag.strip()
    if not t:
        return "Tag is empty."
    if not TAG_PATTERN.match(t):
        slug = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
        return (
            f"{tag!r} does not match the vault's tag convention (lowercase kebab-case, "
            f"e.g. 'machine-learning'). Did you mean {slug!r}?"
        )
    return None
