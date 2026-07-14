"""Filesystem-native Obsidian vault access: index, link graph, frontmatter, safe writes.

Reads the vault as plain markdown on disk. No Obsidian process, no database.
The vault is a Syncthing replica, so every write must be atomic (temp + rename)
or a write racing Obsidian-on-another-machine produces sync-conflict files.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

# Directories and files that are never notes. Syncthing and Obsidian both keep
# state inside the vault root; indexing it would surface junk and, worse, let a
# write land in .stversions where Syncthing would fight us over it.
IGNORED_DIRS = {".obsidian", ".stversions", ".stfolder", ".git", ".trash"}
CONFLICT_MARKER = "sync-conflict"
TRASH_DIR = ".trash"

# Obsidian resolves [[Target]] with an optional #heading, ^block, and |alias.
# Embeds (![[...]]) are the same syntax with a leading bang.
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]]+?)\]\]")

# Fenced code blocks. Tag/link scanning must skip these, or code comments and
# LaTeX (`\begin{align}` reads as the tag #align) register as vault content.
FENCE_CODE_RE = re.compile(r"^\s*(```|~~~)")

# Self-contained display math on one line: $$ x = 1 $$. These must NOT be treated
# as opening a block -- doing so swallows the rest of the note and silently drops
# every link after it, which cost 95 of this vault's 380 links before it was fixed.
MATH_SPAN_RE = re.compile(r"\$\$.*?\$\$")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # never re-wrap long lines in existing notes


class VaultError(Exception):
    """Raised for caller-fixable problems. Message is shown to the agent."""


@dataclass(frozen=True)
class Link:
    """One [[wikilink]] occurrence."""

    target: str  # note name or path, no heading/block/alias
    heading: str | None = None
    block: str | None = None
    alias: str | None = None
    embed: bool = False
    line: int = 0

    @property
    def raw(self) -> str:
        s = self.target
        if self.heading:
            s += f"#{self.heading}"
        if self.block:
            s += f"^{self.block}"
        if self.alias:
            s += f"|{self.alias}"
        return ("![[" if self.embed else "[[") + s + "]]"


@dataclass
class Note:
    rel: str  # vault-relative POSIX path, e.g. "03 🛠 Projects/Loimi/x.md"
    frontmatter: CommentedMap
    body: str
    links: list[Link]
    mtime: float
    size: int

    @property
    def stem(self) -> str:
        return PurePosixPath(self.rel).stem

    @property
    def is_excalidraw(self) -> bool:
        # Excalidraw notes are .md but carry a megabyte of embedded JSON/base64.
        # They belong in the link graph but must stay out of content search.
        return "excalidraw-plugin" in self.frontmatter

    @property
    def tags(self) -> list[str]:
        return _normalize_tags(self.frontmatter.get("tags"))

    @property
    def aliases(self) -> list[str]:
        raw = self.frontmatter.get("aliases") or self.frontmatter.get("alias")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw.strip()] if raw.strip() else []
        return [str(a).strip() for a in raw if str(a).strip()]


def _normalize_tags(raw) -> list[str]:
    """Frontmatter tags appear as a list, a comma string, or with a leading '#'."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", " ").split()]
    else:
        parts = [str(t).strip() for t in raw]
    return [p.lstrip("#") for p in parts if p and p.strip("#")]


def split_frontmatter(text: str) -> tuple[CommentedMap, str]:
    """Split a note into (frontmatter, body). Body excludes the --- fences.

    A note without frontmatter yields an empty map, so callers never special-case.
    """
    if not text.startswith("---"):
        return CommentedMap(), text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return CommentedMap(), text
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            try:
                fm = _yaml.load(raw) if raw.strip() else CommentedMap()
            except Exception:
                # Malformed YAML is the author's business, not ours. Treat the
                # note as bodyless-frontmatter rather than losing the content.
                return CommentedMap(), text
            if fm is None:
                fm = CommentedMap()
            if not isinstance(fm, CommentedMap):
                return CommentedMap(), text
            return fm, body
    return CommentedMap(), text


def join_frontmatter(fm: CommentedMap, body: str) -> str:
    """Rebuild a note, preserving key order and formatting of existing keys."""
    if not fm:
        return body
    buf = io.StringIO()
    _yaml.dump(fm, buf)
    return f"---\n{buf.getvalue()}---\n{body.lstrip(chr(10))}"


def strip_fences(body: str) -> str:
    """Blank out fenced code and display math, so link/tag scans see only prose.

    Math is stripped span-wise rather than line-wise: a link sitting on the same
    line as an equation survives. An odd number of `$$` on a line opens a block;
    an even number is self-contained and closes itself.
    """
    out: list[str] = []
    in_code = False
    in_math = False

    for line in body.split("\n"):
        if FENCE_CODE_RE.match(line):
            in_code = not in_code
            out.append("")
            continue
        if in_code:
            out.append("")
            continue

        if in_math:
            if "$$" in line:
                in_math = False
                out.append(line.split("$$", 1)[1])  # keep prose after the closing $$
            else:
                out.append("")
            continue

        line = MATH_SPAN_RE.sub(" ", line)
        if line.count("$$") % 2 == 1:
            head, _ = line.split("$$", 1)
            in_math = True
            out.append(head)  # keep prose before the opening $$
            continue

        out.append(line)

    return "\n".join(out)


def _frontmatter_links(fm: CommentedMap) -> list[Link]:
    """Wikilinks living in frontmatter values, e.g. `source: "[[Some Note]]"`.

    Obsidian resolves these, so they belong in the graph.
    """
    found: list[Link] = []

    def walk(value) -> None:
        if isinstance(value, str):
            found.extend(parse_links(value))
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    for v in fm.values():
        walk(v)
    return found


def parse_links(body: str) -> list[Link]:
    links: list[Link] = []
    for lineno, line in enumerate(strip_fences(body).split("\n"), start=1):
        for embed, inner in WIKILINK_RE.findall(line):
            target, alias = (inner.split("|", 1) + [None])[:2] if "|" in inner else (inner, None)
            block = heading = None
            if "^" in target:
                target, block = target.split("^", 1)
            if "#" in target:
                target, heading = target.split("#", 1)
            target = target.strip()
            if not target:
                continue  # [[#heading]] is a same-note link; no edge to draw
            links.append(
                Link(
                    target=target,
                    heading=heading.strip() if heading else None,
                    block=block.strip() if block else None,
                    alias=alias.strip() if alias else None,
                    embed=embed == "!",
                    line=lineno,
                )
            )
    return links


class Vault:
    """An indexed view of the vault. Rebuilt lazily from mtimes on each access."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise VaultError(f"Vault path is not a directory: {self.root}")
        self._notes: dict[str, Note] = {}
        self._backlinks: dict[str, set[str]] = {}
        self._stems: dict[str, list[str]] = {}
        self._aliases: dict[str, list[str]] = {}
        self._unresolved: dict[str, set[str]] = {}
        self._indexed = False

    # --- paths -------------------------------------------------------------

    def resolve_path(self, rel: str) -> Path:
        """Map a vault-relative path to an absolute one, refusing to escape the vault.

        Guards traversal (`..`), absolute paths, and symlinks pointing outside.
        """
        if not rel or not rel.strip():
            raise VaultError("Path is empty.")
        raw = rel.strip()

        # Test absoluteness BEFORE any normalisation. Stripping the leading slash
        # first would quietly turn "/etc/passwd" into the in-vault "etc/passwd"
        # and create it -- contained, but not what the caller asked for.
        if raw.startswith(("/", "\\")) or PurePosixPath(raw).is_absolute():
            raise VaultError(f"Path must be vault-relative, not absolute: {rel!r}")

        p = PurePosixPath(raw)
        if any(part == ".." for part in p.parts):
            raise VaultError(f"Path escapes the vault: {rel!r}")
        full = (self.root / Path(*p.parts)).resolve()
        if full != self.root and self.root not in full.parents:
            raise VaultError(f"Path escapes the vault: {rel!r}")
        return full

    def _rel(self, full: Path) -> str:
        return full.relative_to(self.root).as_posix()

    def _is_ignored(self, full: Path) -> bool:
        rel_parts = full.relative_to(self.root).parts
        if any(part in IGNORED_DIRS for part in rel_parts):
            return True
        return CONFLICT_MARKER in full.name

    # --- index -------------------------------------------------------------

    def refresh(self) -> None:
        """Re-stat the vault and re-parse only notes whose mtime or size changed."""
        seen: set[str] = set()
        for full in self.root.rglob("*.md"):
            if not full.is_file() or self._is_ignored(full):
                continue
            rel = self._rel(full)
            seen.add(rel)
            st = full.stat()
            cached = self._notes.get(rel)
            if cached and cached.mtime == st.st_mtime and cached.size == st.st_size:
                continue
            try:
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm, body = split_frontmatter(text)
            self._notes[rel] = Note(
                rel=rel,
                frontmatter=fm,
                body=body,
                links=parse_links(body) + _frontmatter_links(fm),
                mtime=st.st_mtime,
                size=st.st_size,
            )

        for gone in set(self._notes) - seen:
            del self._notes[gone]

        self._reindex()
        self._indexed = True

    def _reindex(self) -> None:
        self._stems, self._aliases = {}, {}
        for rel, note in self._notes.items():
            self._stems.setdefault(note.stem.lower(), []).append(rel)
            for a in note.aliases:
                self._aliases.setdefault(a.lower(), []).append(rel)

        self._backlinks = {rel: set() for rel in self._notes}
        self._unresolved = {}
        for rel, note in self._notes.items():
            for link in note.links:
                target = self.resolve_link(link.target)
                if target:
                    self._backlinks[target].add(rel)
                else:
                    self._unresolved.setdefault(rel, set()).add(link.target)

    def _ensure(self) -> None:
        self.refresh()

    def resolve_link(self, target: str) -> str | None:
        """Resolve a wikilink target to a note path, the way Obsidian does.

        Obsidian matches by name, not full path, so `[[Kanerva]]` finds the note
        wherever it lives. A path-ish target is tried as a path first. When a bare
        name is ambiguous we take the shortest path, which is deterministic and
        matches Obsidian's preference for the least-nested match.
        """
        t = target.strip().removesuffix(".md")
        if not t:
            return None

        if "/" in t:
            candidate = f"{t}.md"
            if candidate in self._notes:
                return candidate
            # A path that doesn't exist may still name a note by its stem.

        by_stem = self._stems.get(PurePosixPath(t).stem.lower(), [])
        if by_stem:
            return min(by_stem, key=lambda r: (r.count("/"), len(r), r))

        by_alias = self._aliases.get(t.lower(), [])
        if by_alias:
            return min(by_alias, key=lambda r: (r.count("/"), len(r), r))

        return None

    # --- reads -------------------------------------------------------------

    def notes(self) -> dict[str, Note]:
        self._ensure()
        return self._notes

    def get(self, rel: str) -> Note:
        """Fetch a note by path or by name, with a useful error when it's missing."""
        self._ensure()
        key = rel.strip().lstrip("/")
        if not key.endswith(".md"):
            key_md = f"{key}.md"
        else:
            key_md = key
        if key_md in self._notes:
            return self._notes[key_md]

        resolved = self.resolve_link(key)
        if resolved:
            return self._notes[resolved]

        raise VaultError(_not_found_message(key, list(self._notes)))

    def backlinks(self, rel: str) -> list[str]:
        note = self.get(rel)
        return sorted(self._backlinks.get(note.rel, set()))

    def unresolved(self, rel: str) -> list[str]:
        note = self.get(rel)
        return sorted(self._unresolved.get(note.rel, set()))

    def outgoing(self, rel: str) -> list[tuple[Link, str | None]]:
        """Outgoing links paired with the note each resolves to (None if unresolved)."""
        note = self.get(rel)
        return [(link, self.resolve_link(link.target)) for link in note.links]

    def neighborhood(self, rel: str, depth: int = 1) -> dict[str, int]:
        """Notes within `depth` hops, following links in either direction."""
        note = self.get(rel)
        seen = {note.rel: 0}
        frontier = [note.rel]
        for d in range(1, depth + 1):
            nxt = []
            for cur in frontier:
                outs = {r for _, r in self.outgoing(cur) if r}
                ins = self._backlinks.get(cur, set())
                for n in outs | ins:
                    if n not in seen:
                        seen[n] = d
                        nxt.append(n)
            frontier = nxt
            if not frontier:
                break
        del seen[note.rel]
        return seen

    # --- search ------------------------------------------------------------

    def search(
        self,
        query: str | None = None,
        *,
        regex: bool = False,
        path_glob: str | None = None,
        tag: str | None = None,
        frontmatter: dict | None = None,
        context_lines: int = 1,
        case_sensitive: bool = False,
    ) -> list[dict]:
        """Grep the vault. Filters compose: every supplied one must match.

        Searches note bodies and frontmatter values. Excalidraw note bodies are
        skipped -- they are megabytes of embedded JSON and would swamp results --
        but their frontmatter still matches.
        """
        self._ensure()

        pattern = None
        if query:
            pattern = re.compile(
                query if regex else re.escape(query),
                0 if case_sensitive else re.IGNORECASE,
            )

        results: list[dict] = []
        for rel in sorted(self._notes):
            note = self._notes[rel]

            if path_glob and not PurePosixPath(rel).match(path_glob):
                continue
            if tag and tag.lstrip("#").lower() not in [t.lower() for t in note.tags]:
                continue
            if frontmatter and not _fm_matches(note.frontmatter, frontmatter):
                continue

            if not pattern:
                # Pure metadata query: the note itself is the hit.
                results.append({"path": rel, "matches": [], "frontmatter": dict(note.frontmatter)})
                continue

            matches = []
            fm_hit = any(pattern.search(str(v)) for v in note.frontmatter.values())

            if not note.is_excalidraw:
                lines = note.body.split("\n")
                for i, line in enumerate(lines):
                    if pattern.search(line):
                        lo = max(0, i - context_lines)
                        hi = min(len(lines), i + context_lines + 1)
                        matches.append(
                            {
                                "line": i + 1,
                                "text": line.strip(),
                                "context": "\n".join(lines[lo:hi]),
                            }
                        )

            if matches or fm_hit:
                results.append(
                    {
                        "path": rel,
                        "matches": matches,
                        "frontmatter_match": fm_hit,
                        "frontmatter": dict(note.frontmatter),
                    }
                )

        return results

    # --- writes ------------------------------------------------------------

    def write_atomic(self, rel: str, text: str) -> str:
        """Write a note atomically: temp file in the same dir, fsync, then rename.

        os.replace is atomic within a filesystem, so Syncthing and Obsidian only
        ever observe the old or the new file -- never a half-written one. A plain
        open(w) here would be the single most likely source of sync conflicts.
        """
        full = self.resolve_path(rel)
        if full.suffix != ".md":
            raise VaultError(f"Only .md notes can be written; got {full.name!r}.")
        full.parent.mkdir(parents=True, exist_ok=True)

        tmp = full.parent / f".{full.name}.miradian.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, full)
        finally:
            tmp.unlink(missing_ok=True)

        self.refresh()
        return self._rel(full)

    def trash(self, rel: str) -> str:
        """Move a note to .trash/ instead of unlinking it.

        .trash is in the vault's .stignore, so the deletion still propagates to
        other machines while a recovery copy stays on this one.
        """
        note = self.get(rel)
        full = self.resolve_path(note.rel)
        dest = self.root / TRASH_DIR / note.rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}.{int(time.time())}{dest.suffix}")
        shutil.move(str(full), str(dest))
        self.refresh()
        return dest.relative_to(self.root).as_posix()


def _fm_matches(fm: CommentedMap, wanted: dict) -> bool:
    """Every wanted key must be present and equal (case-insensitive on strings).

    A wanted value matching against a list field (e.g. tags) succeeds on membership.
    """
    for k, want in wanted.items():
        if k not in fm:
            return False
        have = fm[k]
        if isinstance(have, list):
            if not any(str(h).strip().lower() == str(want).strip().lower() for h in have):
                return False
        elif str(have).strip().lower() != str(want).strip().lower():
            return False
    return True


def _not_found_message(key: str, known: list[str]) -> str:
    """An error that tells the agent what to do next, not just that it failed."""
    stem = PurePosixPath(key).stem.lower()
    near = [r for r in known if stem in PurePosixPath(r).stem.lower()][:5]
    if near:
        listed = ", ".join(repr(n) for n in near)
        return f"Note {key!r} not found. Similarly named notes: {listed}. Use vault_search to locate it."
    return f"Note {key!r} not found. Use vault_search to find it, or vault_list_notes to browse folders."
