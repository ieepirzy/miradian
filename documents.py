"""Generic structured-document access: projects, documents, metadata, identity.

This is the layer a non-Obsidian consumer talks to. The abstraction is *Markdown
files with structured metadata*, nothing more: no wikilinks, no vault schema, no
tag registry, no `.obsidian`. Those live one layer down in `vault.py` and one layer
up in `server.py`'s MCP tools, and neither is visible from here.

Two ideas carry the whole module:

  * **A project is a directory.** Any directory directly under a configured project
    root is a project, empty or not, so starting one is `mkdir`.
  * **A document is managed when it says so.** A Markdown file is project state only
    if its frontmatter carries the marker field (`doc_type` by default). Everything
    else in the tree is ordinary knowledge and is left alone. The consumer owns that
    vocabulary; this module only tests for the field's presence.

Identity is the `uid` in a managed document's frontmatter, and location is its path.
They are separate: a human dragging a file in Obsidian changes the path and not the
identity, which is the entire point.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

from ruamel.yaml.comments import CommentedMap

from vault import (
    IGNORED_DIRS,
    Note,
    Vault,
    VaultError,
    content_hash,
    has_frontmatter_fence,
    join_frontmatter,
    split_frontmatter,
)

logger = logging.getLogger("miradian.documents")

DEFAULT_DOC_TYPE_FIELD = "doc_type"
DEFAULT_UID_FIELD = "uid"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
#
# Each carries the HTTP status its transport should use, so api.py never has to
# re-derive it from the exception type and the MCP layer can ignore it entirely.


class DocumentError(Exception):
    status = 400
    code = "bad_request"

    def details(self) -> dict:
        return {}


class NotFound(DocumentError):
    status = 404
    code = "not_found"


class AlreadyExists(DocumentError):
    status = 409
    code = "already_exists"


class Conflict(DocumentError):
    """The document changed since the caller last read it."""

    status = 409
    code = "version_conflict"

    def __init__(self, message: str, current_version: str):
        super().__init__(message)
        self.current_version = current_version

    def details(self) -> dict:
        return {"current_version": self.current_version}


class Malformed(DocumentError):
    """The file on disk is not something we can safely rewrite."""

    status = 422
    code = "malformed_document"


class OutOfScope(DocumentError):
    status = 403
    code = "out_of_scope"


class PolicyRejected(DocumentError):
    """A caller-supplied validation policy refused the change."""

    status = 422
    code = "policy_rejected"

    def __init__(self, message: str, errors: list[str]):
        super().__init__(message)
        self.errors = errors

    def details(self) -> dict:
        return {"errors": self.errors}


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    id: str          # vault-relative directory path; stable while the folder exists
    name: str        # the directory name, which is what the human sees in Obsidian
    path: str
    root: str        # the configured project root it was found under
    document_count: int          # managed documents only
    updated_at: str | None       # newest managed-document mtime, ISO-8601 UTC

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DocumentRef:
    """A document without its body. What listings and change feeds return."""

    id: str | None               # uid: identity, immutable, None if unmanaged
    path: str                    # location, mutable
    project: str | None          # project id, or None for a loose file under a root
    doc_type: str | None
    title: str | None
    metadata: dict
    version: str
    mtime: str
    structured: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Document(DocumentRef):
    body: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def plain(value: Any) -> Any:
    """Convert ruamel's YAML types into something json.dumps will accept.

    ruamel returns CommentedMap/CommentedSeq (dict/list subclasses, fine) but also
    date and datetime objects for unquoted YAML dates, which json refuses. A vault
    is full of `date: 2026-04-07`, so this is not an edge case.
    """
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # datetime.date, ruamel's scalar date types
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def apply_metadata(fm: CommentedMap, set_fields: dict | None, unset_fields: list[str] | None) -> list[str]:
    """Apply a set/unset patch to a frontmatter map in place.

    Returns the names asked to be unset that were not there. Shared by the MCP tool
    and the service so the patch semantics exist once -- deliberately pure, with no
    validation and no I/O, because those differ between the two surfaces and this
    does not.
    """
    for k, v in (set_fields or {}).items():
        fm[k] = v
    missing = [k for k in (unset_fields or []) if k not in fm]
    for k in unset_fields or []:
        fm.pop(k, None)
    return missing


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _as_text(value: Any) -> str | None:
    """Best-effort single string out of a frontmatter value.

    The real vault has `title:` written both as a scalar and as a one-item list, so
    a consumer asking for a title should not have to handle both.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return _as_text(value[0]) if value else None
    return str(value)


def _new_uid() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DocumentService:
    """Structured-document access over a Vault.

    Reads and writes address any vault-relative path -- the MCP tools need the whole
    vault. *Discovery* (projects, listings, the change feed) is scoped to the
    configured project roots. Callers that need a narrower write scope enforce it
    themselves; `path_in_scope` is provided for exactly that.
    """

    def __init__(
        self,
        vault: Vault,
        roots: list[str] | None = None,
        *,
        doc_type_field: str = DEFAULT_DOC_TYPE_FIELD,
        uid_field: str = DEFAULT_UID_FIELD,
        assign_uid: bool = True,
    ):
        self.vault = vault
        self.roots = [r.strip().strip("/") for r in (roots or []) if r.strip().strip("/")]
        self.doc_type_field = doc_type_field
        self.uid_field = uid_field
        self.assign_uid = assign_uid
        self._stamping = False  # re-entrancy guard: stamping writes, writes re-index

    # --- classification ----------------------------------------------------

    def is_managed(self, note: Note) -> bool:
        """Does this note declare itself project state?"""
        value = note.frontmatter.get(self.doc_type_field)
        return value is not None and str(value).strip() != ""

    def root_of(self, path: str) -> str | None:
        for root in self.roots:
            if path == root or path.startswith(root + "/"):
                return root
        return None

    def path_in_scope(self, path: str) -> bool:
        """Is this path inside a configured project root?

        The HTTP API uses this to keep a project-state consumer out of the rest of
        the vault. It is a scope check, not a containment check -- `Vault.resolve_path`
        still does the latter, and both run.
        """
        return self.root_of(path) is not None

    def project_of(self, path: str) -> str | None:
        """The project a document belongs to: its first directory under a root."""
        root = self.root_of(path)
        if root is None:
            return None
        rest = PurePosixPath(path[len(root) :].lstrip("/"))
        if len(rest.parts) < 2:
            return None  # a loose file sitting directly in the root
        return f"{root}/{rest.parts[0]}"

    # --- indexing ----------------------------------------------------------

    def _notes(self) -> dict[str, Note]:
        """The current note index, with uids converged first.

        Stamping happens here rather than in a background job so there is exactly
        one code path and no daemon to supervise. It writes only to documents that
        already declared `doc_type`, and only once each, so steady-state reads do
        no I/O beyond the index refresh they were doing anyway.
        """
        notes = self.vault.notes()
        if self.assign_uid and not self._stamping:
            if self._converge_uids(notes):
                notes = self.vault.notes()
        return notes

    def _converge_uids(self, notes: dict[str, Note]) -> bool:
        """Give every managed document a unique uid. Returns True if anything changed.

        Two repairs, both idempotent:
          * a managed document with no uid gets one;
          * a uid claimed by more than one document stays with the *oldest* file and
            the others are re-stamped.

        The duplicate case is not hypothetical -- duplicating a note in Obsidian
        copies its frontmatter, uid and all -- and silently letting two documents
        share an identity would make the change feed alternate between them.

        Oldest-wins is what keeps the original's identity intact: the copy is the
        new file, so it is the one that should get the new uid. Path order would
        decide it alphabetically, which hands "plan copy.md" the identity of
        "plan.md" purely because a space sorts before a dot.
        """
        managed = [rel for rel, note in notes.items() if self.root_of(rel) and self.is_managed(note)]
        managed.sort(key=lambda rel: (notes[rel].mtime, rel))
        seen: dict[str, str] = {}
        need: list[str] = []

        for rel in managed:
            uid = _as_text(notes[rel].frontmatter.get(self.uid_field))
            uid = uid.strip() if uid else ""
            if not uid:
                need.append(rel)
            elif uid in seen:
                logger.warning(
                    "duplicate %s %r on %r and %r; re-stamping the later one",
                    self.uid_field, uid, seen[uid], rel,
                )
                need.append(rel)
            else:
                seen[uid] = rel

        if not need:
            return False

        self._stamping = True
        try:
            for rel in need:
                try:
                    self._stamp_uid(rel)
                except (VaultError, OSError, Malformed) as e:
                    # One unwritable document must not stop the rest from
                    # converging, and it must not fail the caller's read.
                    logger.warning("could not stamp %s on %r: %s", self.uid_field, rel, e)
        finally:
            self._stamping = False
        return True

    def _stamp_uid(self, rel: str) -> str:
        note = self.vault.get(rel)
        self._guard_rewritable(note)
        uid = _new_uid()
        fm = note.frontmatter
        fm[self.uid_field] = uid
        self.vault.write_atomic(note.rel, join_frontmatter(fm, note.body))
        return uid

    def _guard_rewritable(self, note: Note) -> None:
        """Refuse to rebuild a file whose frontmatter we could not parse.

        `split_frontmatter` falls back to (empty map, whole text) on malformed YAML.
        Writing a fresh block onto that would prepend a *second* frontmatter block
        above the broken one -- turning a file the human can still fix by hand into
        one they cannot.
        """
        if not note.frontmatter:
            raw = join_frontmatter(note.frontmatter, note.body)
            if has_frontmatter_fence(raw):
                raise Malformed(
                    f"{note.rel!r} opens with a `---` fence but its YAML frontmatter does not "
                    "parse. Fix it by hand; rewriting it here would add a second block."
                )

    # --- refs --------------------------------------------------------------

    def _ref(self, note: Note, *, body: bool = False) -> DocumentRef:
        fm = note.frontmatter
        doc_type = _as_text(fm.get(self.doc_type_field)) if self.is_managed(note) else None
        uid = _as_text(fm.get(self.uid_field))
        common = dict(
            id=(uid.strip() if uid and uid.strip() else None),
            path=note.rel,
            project=self.project_of(note.rel),
            doc_type=doc_type,
            title=_as_text(fm.get("title")) or PurePosixPath(note.rel).stem,
            metadata=plain(dict(fm)),
            version=note.hash or content_hash(join_frontmatter(fm, note.body)),
            mtime=_iso(note.mtime),
            structured=doc_type is not None,
        )
        return Document(**common, body=note.body) if body else DocumentRef(**common)

    # --- discovery ---------------------------------------------------------

    def discover_projects(self) -> list[Project]:
        """Every directory directly under a configured root, in path order.

        A project with no documents still appears: `mkdir` is the whole ceremony for
        starting one, and a project that only becomes visible once it has content
        would make that false.
        """
        notes = self._notes()
        projects: list[Project] = []

        for root in self.roots:
            try:
                root_full = self.vault.resolve_path(root)
            except VaultError:
                continue
            if not root_full.is_dir():
                continue

            for child in sorted(root_full.iterdir(), key=lambda p: p.name):
                if not child.is_dir() or child.name in IGNORED_DIRS:
                    continue
                pid = f"{root}/{child.name}"
                docs = [n for rel, n in notes.items() if self.project_of(rel) == pid and self.is_managed(n)]
                projects.append(
                    Project(
                        id=pid,
                        name=child.name,
                        path=pid,
                        root=root,
                        document_count=len(docs),
                        updated_at=_iso(max(n.mtime for n in docs)) if docs else None,
                    )
                )
        return projects

    def list_folders(self, project: str | None = None) -> list[str]:
        """Directories under a project (or under every root, if none is given).

        Returned as vault-relative paths so they can be handed straight back as a
        `path_prefix` filter or used to build a new document's path.
        """
        bases = [project.strip("/")] if project else list(self.roots)
        out: list[str] = []
        for base in bases:
            try:
                full = self.vault.resolve_path(base)
            except VaultError:
                continue
            if not full.is_dir():
                continue
            for sub in sorted(full.rglob("*")):
                if not sub.is_dir():
                    continue
                rel = sub.relative_to(self.vault.root).as_posix()
                if any(part in IGNORED_DIRS for part in PurePosixPath(rel).parts):
                    continue
                out.append(rel)
        return out

    def list_documents(
        self,
        project: str | None = None,
        *,
        doc_type: str | None = None,
        include_unstructured: bool = False,
        path_prefix: str | None = None,
    ) -> list[DocumentRef]:
        """Documents under the project roots, managed ones by default.

        `include_unstructured=True` is what makes coexistence work in both
        directions: the default listing hides ordinary project knowledge from a
        project-state consumer, and the flag lets a caller that wants to browse the
        tree see everything.
        """
        notes = self._notes()
        prefix = path_prefix.strip("/") + "/" if path_prefix else None
        scope = project.strip("/") if project else None

        refs: list[DocumentRef] = []
        for rel in sorted(notes):
            if not self.root_of(rel):
                continue
            if scope and not (rel == scope or rel.startswith(scope + "/")):
                continue
            if prefix and not rel.startswith(prefix):
                continue
            note = notes[rel]
            managed = self.is_managed(note)
            if not managed and not include_unstructured:
                continue
            ref = self._ref(note)
            if doc_type and (ref.doc_type or "").strip().lower() != doc_type.strip().lower():
                continue
            refs.append(ref)
        return refs

    def managed_documents(self) -> list[DocumentRef]:
        """Every managed document under the roots. The change feed's input."""
        return self.list_documents()

    # --- addressing --------------------------------------------------------

    def _by_uid(self, uid: str) -> Note:
        notes = self._notes()
        matches = [
            rel
            for rel, note in notes.items()
            if (_as_text(note.frontmatter.get(self.uid_field)) or "").strip() == uid
        ]
        if not matches:
            raise NotFound(f"No document with {self.uid_field} {uid!r}.")
        # _converge_uids makes duplicates transient, but a read racing a stamp can
        # still see two. Shortest path wins, deterministically.
        return notes[min(matches, key=lambda r: (r.count("/"), len(r), r))]

    def resolve(self, id: str | None = None, path: str | None = None) -> Note:
        """Locate a document by uid (preferred) or by exact path.

        Path lookup is exact and vault-relative. It deliberately does NOT use
        `Vault.get`'s bare-name resolution: matching `[[Tensor]]` to a note called
        Tensor wherever it sits is an Obsidian rule, and letting it decide which
        file an API write lands in would be a surprising way to lose data.
        """
        if id:
            return self._by_uid(id.strip())
        if path:
            rel = path.strip().lstrip("/")
            if not rel.endswith(".md"):
                rel += ".md"
            notes = self._notes()
            if rel not in notes:
                raise NotFound(f"No document at path {rel!r}.")
            return notes[rel]
        raise DocumentError("Supply either id or path.")

    def read_document(self, id: str | None = None, path: str | None = None) -> Document:
        return self._ref(self.resolve(id, path), body=True)  # type: ignore[return-value]

    # --- writes ------------------------------------------------------------

    def _check_version(self, note: Note, expected_version: str | None) -> None:
        if expected_version and note.hash != expected_version:
            raise Conflict(
                f"{note.rel!r} has changed since you read it "
                f"(expected {expected_version}, found {note.hash}). Re-read and retry.",
                current_version=note.hash,
            )

    def create_document(
        self,
        path: str,
        body: str = "",
        metadata: dict | None = None,
    ) -> Document:
        """Create a new document. Fails if the path is taken.

        A uid is assigned here rather than left to the next scan, so the caller gets
        an identity back in the same round trip it created the document in.
        """
        rel = path.strip().lstrip("/")
        if not rel.endswith(".md"):
            rel += ".md"
        try:
            if self.vault.resolve_path(rel).exists():
                raise AlreadyExists(f"A document already exists at {rel!r}.")
        except VaultError as e:
            raise DocumentError(str(e)) from e

        fm = CommentedMap()
        for k, v in (metadata or {}).items():
            fm[k] = v
        if self.assign_uid and self.is_managed(_fake_note(rel, fm)) and not fm.get(self.uid_field):
            fm[self.uid_field] = _new_uid()

        return self._commit(rel, fm, body)

    def write_document(
        self,
        id: str | None = None,
        path: str | None = None,
        *,
        body: str,
        metadata: dict | None = None,
        expected_version: str | None = None,
    ) -> Document:
        """Replace an existing document's body, and its metadata if supplied.

        Omitting `metadata` keeps the document's existing frontmatter untouched --
        so a consumer that only renders and edits prose cannot destroy metadata it
        never looked at. Passing `metadata` replaces the block wholesale; use
        `patch_metadata` to change individual fields.
        """
        note = self.resolve(id, path)
        self._check_version(note, expected_version)
        self._guard_rewritable(note)

        if metadata is None:
            fm = note.frontmatter
        else:
            fm = CommentedMap()
            for k, v in metadata.items():
                fm[k] = v
            # Identity is not the caller's to reassign. Keeping the existing uid
            # across a wholesale metadata replacement is what makes "replace the
            # metadata" safe to expose at all.
            existing = _as_text(note.frontmatter.get(self.uid_field))
            if existing:
                fm[self.uid_field] = existing

        return self._commit(note.rel, fm, body)

    def patch_metadata(
        self,
        id: str | None = None,
        path: str | None = None,
        *,
        set_fields: dict | None = None,
        unset_fields: list[str] | None = None,
        expected_version: str | None = None,
        policy: Callable[[CommentedMap, str], list[str]] | None = None,
    ) -> tuple[Document, list[str]]:
        """Change frontmatter fields, leaving body and key order intact.

        Returns the document and the names that were asked to be unset but were not
        present. `policy` is a validation hook -- the MCP surface passes the vault's
        frontmatter schema, the HTTP surface passes nothing -- so the patch mechanics
        live here once while the two surfaces keep their own rules.
        """
        if not set_fields and not unset_fields:
            raise DocumentError("Nothing to do: supply set, unset, or both.")

        note = self.resolve(id, path)
        self._check_version(note, expected_version)
        self._guard_rewritable(note)

        fm = note.frontmatter
        if self.uid_field in (unset_fields or []):
            raise DocumentError(f"{self.uid_field!r} is the document's identity and cannot be unset.")
        missing = apply_metadata(fm, set_fields, unset_fields)

        if policy:
            errors = policy(fm, note.rel)
            if errors:
                raise PolicyRejected("The change was refused by the active policy.", errors)

        if self.assign_uid and self.is_managed(_fake_note(note.rel, fm)) and not fm.get(self.uid_field):
            fm[self.uid_field] = _new_uid()

        return self._commit(note.rel, fm, note.body), missing

    def move_document(
        self,
        id: str | None = None,
        path: str | None = None,
        *,
        to: str,
        expected_version: str | None = None,
    ) -> Document:
        """Move or rename a document. The uid does not change; the path does."""
        note = self.resolve(id, path)
        self._check_version(note, expected_version)

        dest = to.strip().lstrip("/")
        if not dest.endswith(".md"):
            dest += ".md"
        try:
            new_rel = self.vault.move(note.rel, dest)
        except VaultError as e:
            raise DocumentError(str(e)) from e

        return self._ref(self.vault.notes()[new_rel], body=True)  # type: ignore[return-value]

    def delete_document(
        self,
        id: str | None = None,
        path: str | None = None,
        *,
        expected_version: str | None = None,
    ) -> dict:
        """Remove a document. Whether that is recoverable is the vault's business."""
        note = self.resolve(id, path)
        self._check_version(note, expected_version)
        uid = _as_text(note.frontmatter.get(self.uid_field))
        try:
            dest = self.vault.trash(note.rel)
        except VaultError as e:
            raise DocumentError(str(e)) from e
        return {"id": uid, "path": note.rel, "deleted": True, "recoverable_at": dest}

    def _commit(self, rel: str, fm: CommentedMap, body: str) -> Document:
        try:
            written = self.vault.write_atomic(rel, join_frontmatter(fm, body))
        except VaultError as e:
            raise DocumentError(str(e)) from e
        except OSError as e:
            raise DocumentError(f"Could not write {rel!r}: {e}") from e
        return self._ref(self.vault.notes()[written], body=True)  # type: ignore[return-value]


def _fake_note(rel: str, fm: CommentedMap) -> Note:
    """A Note stand-in, so is_managed() can be asked about frontmatter not yet on disk."""
    return Note(rel=rel, frontmatter=fm, body="", links=[], mtime=0.0, size=0)
