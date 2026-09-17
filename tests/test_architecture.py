"""The one architectural claim this whole design rests on, made falsifiable.

"Obsidian semantics terminate inside Miradian" is asserted in the README, in the
proposal, and in the API reference. Until now nothing enforced it, so the day
someone reached for the link graph from `documents.py` because it was right there
and convenient, three documents would quietly have become wrong.

These walk the AST of the generic layer and fail on the commit that breaks the
claim, rather than months later when a consumer notices Obsidian in a payload.

Every check here is mutation-tested (see the bottom of this file): a structural
test that cannot fail is worse than no test, because it reads like coverage.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The generic layer: what a non-Obsidian consumer's requests pass through.
GENERIC_MODULES = ["documents.py", "changes.py", "api.py"]

# Obsidian's, not Markdown's. `[[Target]]` resolving by stem with shallowest-wins
# is a rule from one editor, and `Vault.get` applies it to any bare name.
OBSIDIAN_VAULT_API = {
    "get",            # bare-name resolution -- use resolve()/notes[path] instead
    "backlinks",
    "outgoing",
    "neighborhood",
    "resolve_link",
    "search",         # Obsidian-flavoured hit shapes; not exposed generically
    "tag_counts",
}

OBSIDIAN_NAMES = {"Link", "parse_links", "strip_fences", "WIKILINK_RE", "CONFLICT_MARKER"}


def parse(name: str) -> ast.Module:
    return ast.parse((REPO / name).read_text(encoding="utf-8"), filename=name)


def imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def imported_names(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def vault_method_calls(tree: ast.Module) -> set[str]:
    """Methods called on anything named `vault`, however it is reached.

    Matches `vault.x()`, `self.vault.x()` and `service.vault.x()`. Written to catch
    the shapes that actually occur rather than one of them -- a predicate narrow
    enough to miss `self.vault.get()` would pass while proving nothing, which is
    the failure mode these tests exist to avoid.
    """
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        base = node.func.value
        if isinstance(base, ast.Name) and base.id == "vault":
            calls.add(node.func.attr)
        elif isinstance(base, ast.Attribute) and base.attr == "vault":
            calls.add(node.func.attr)
    return calls


# --- the claim -------------------------------------------------------------


@pytest.mark.parametrize("module", GENERIC_MODULES)
def test_the_generic_layer_does_not_use_obsidian_vault_semantics(module):
    used = vault_method_calls(parse(module)) & OBSIDIAN_VAULT_API
    assert not used, (
        f"{module} calls {sorted(used)} on the vault. Those are Obsidian semantics "
        "-- bare-name link resolution, the wikilink graph, Obsidian-shaped search "
        "hits -- and they must not reach a consumer that only asked for Markdown "
        "with frontmatter."
    )


@pytest.mark.parametrize("module", GENERIC_MODULES)
def test_the_generic_layer_does_not_import_the_link_graph(module):
    used = imported_names(parse(module)) & OBSIDIAN_NAMES
    assert not used, f"{module} imports {sorted(used)}, which are wikilink concepts."


@pytest.mark.parametrize("module", GENERIC_MODULES)
def test_the_generic_layer_does_not_import_the_vault_frontmatter_schema(module):
    """Decision 4.1 of the proposal, made structural.

    METADATA.md governs the agent surface. Enforcing it on the service surface
    would leave the API unable to write a single document and would put a
    human-approval prompt in front of a consumer with no human attached. The
    service takes a `policy` callback instead, so the rule is the caller's.
    """
    assert "schema" not in imported_modules(parse(module)), (
        f"{module} imports the vault frontmatter schema. If this is deliberate, "
        "proposal 4.1 and the API reference both need rewriting first."
    )


def test_the_api_layer_does_not_touch_the_filesystem():
    """Path containment lives in Vault.resolve_path and must not be bypassable.

    Every byte the API reads or writes goes through DocumentService and then Vault,
    which is what makes "absolute paths and `..` are rejected" and "writes are
    atomic" true of the HTTP surface rather than merely true somewhere in the repo.
    """
    tree = parse("api.py")
    assert not ({"os", "pathlib", "shutil", "io"} & imported_modules(tree))

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called


def test_the_mcp_layer_is_allowed_what_the_generic_layer_is_not():
    """The counterpart claim: Obsidian semantics are not banned, only contained.

    Without this, the tests above are satisfiable by deleting the features. server.py
    is the agent surface and is *supposed* to be full of vault semantics; this fails
    if the link graph and the schema stop being wired up there at all.
    """
    tree = parse("server.py")
    assert "schema" in imported_modules(tree)
    assert vault_method_calls(tree) & OBSIDIAN_VAULT_API


# --- the checks can fail ---------------------------------------------------


def test_the_detectors_catch_what_they_claim_to(tmp_path):
    """Mutation test for the predicates above, since they are the whole value here.

    Each snippet is a plausible way the boundary would actually be crossed -- the
    convenient one-liner someone adds in a hurry -- rather than a strawman.
    """
    def tree(src: str) -> ast.Module:
        return ast.parse(src)

    # The shape that a narrow predicate would miss: reached through self.
    assert "get" in vault_method_calls(tree("self.vault.get(path)"))
    assert "backlinks" in vault_method_calls(tree("vault.backlinks(rel)"))
    assert "search" in vault_method_calls(tree("service.vault.search(q)"))
    # Not a vault call, and must not be reported as one.
    assert vault_method_calls(tree("notes.get(rel)")) == set()
    assert vault_method_calls(tree("payload.get('id')")) == set()

    assert "schema" in imported_modules(tree("import schema as schema_mod"))
    assert "schema" in imported_modules(tree("from schema import load"))
    assert imported_modules(tree("from documents import DocumentService")) == {"documents"}

    assert "parse_links" in imported_names(tree("from vault import parse_links"))
    assert imported_names(tree("from vault import join_frontmatter")) == {"join_frontmatter"}
