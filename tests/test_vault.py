"""Tests for the vault layer.

Two of these are regressions for bugs found while building against the real
vault, and are the reason this file exists:

  * test_inline_display_math_does_not_swallow_links -- a single-line `$$x$$` was
    read as *opening* a math block, so every link after it in the note vanished.
    That silently ate 95 of the real vault's 380 links.
  * test_absolute_path_is_rejected -- "/etc/passwd" had its leading slash
    stripped and became the in-vault "etc/passwd", which was then created.
"""

from __future__ import annotations

import pytest

from vault import Vault, VaultError, join_frontmatter, parse_links, split_frontmatter

# Folder names deliberately mirror the real vault: emoji, spaces, non-ASCII.
INBOX = "00 📥 Inbox"
KNOWLEDGE = "02 📚 Knowledge/Mathematics"


@pytest.fixture
def vault(tmp_path):
    def write(rel: str, text: str) -> None:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    write(
        f"{KNOWLEDGE}/Linear Algebra.md",
        "---\ntitle: Linear Algebra\ntags:\n- math\nstatus: active\n---\n"
        "# Linear Algebra\n\nSee [[Tensor]] and [[Missing Note]].\n",
    )
    write(f"{KNOWLEDGE}/Tensor.md", "# Tensor\n\nBack to [[Linear Algebra]].\n")
    write(
        f"{INBOX}/MCMC.md",
        "# MCMC\n\n"
        "Energy is $$H(x, p) = -\\log P(x)$$ here.\n\n"
        "Then [[Linear Algebra]] matters.\n\n"
        "```python\n# [[NotALink]] in code\n```\n\n"
        "Also [[Tensor]].\n",
    )
    write(f"{INBOX}/Aliased.md", "---\naliases:\n- The Nickname\n---\n# Aliased\n")
    write(f"{INBOX}/Refs.md", "# Refs\n\nUse [[The Nickname]] here.\n")
    # Same stem in two folders: resolution must prefer the shallower path.
    write("Shared.md", "# Root shared\n")
    write(f"{KNOWLEDGE}/Deep/Shared.md", "# Deep shared\n")
    # Must never be indexed.
    write(".obsidian/workspace.json", "{}")
    write(".stversions/Old.md", "# Old\n")
    write(f"{INBOX}/note.sync-conflict-20260101.md", "# Conflict\n")

    v = Vault(tmp_path)
    v.refresh()
    return v


# --- indexing ---------------------------------------------------------------


def test_ignores_obsidian_syncthing_and_conflict_files(vault):
    rels = list(vault.notes())
    assert not [r for r in rels if ".obsidian" in r or ".stversions" in r or "sync-conflict" in r]
    assert f"{KNOWLEDGE}/Linear Algebra.md" in rels


def test_handles_emoji_and_space_paths(vault):
    note = vault.get(f"{INBOX}/MCMC.md")
    assert note.rel == f"{INBOX}/MCMC.md"
    assert vault.get("MCMC").rel == note.rel  # bare-name lookup


# --- the fence regression ---------------------------------------------------


def test_inline_display_math_does_not_swallow_links(vault):
    """A one-line $$...$$ must not open a math block and eat the rest of the note."""
    targets = [l.target for l in vault.get("MCMC").links]
    assert "Linear Algebra" in targets, "link after inline $$math$$ was dropped"
    assert "Tensor" in targets, "link after a code fence was dropped"


def test_code_fence_contents_are_not_links(vault):
    assert "NotALink" not in [l.target for l in vault.get("MCMC").links]


def test_link_beside_math_on_same_line_survives():
    links = parse_links("See [[Nearby]] where $$x=1$$ holds.")
    assert [l.target for l in links] == ["Nearby"]


def test_multiline_math_block_still_suppressed():
    links = parse_links("$$\nH = 1\n$$\n\n[[After]]")
    assert [l.target for l in links] == ["After"]


# --- link parsing and resolution --------------------------------------------


def test_parses_alias_heading_block_and_embed():
    links = parse_links("[[A|alias]] [[B#heading]] [[C^block]] ![[D]]")
    assert [(l.target, l.alias, l.heading, l.block, l.embed) for l in links] == [
        ("A", "alias", None, None, False),
        ("B", None, "heading", None, False),
        ("C", None, None, "block", False),
        ("D", None, None, None, True),
    ]


def test_resolves_bare_name_regardless_of_folder(vault):
    assert vault.resolve_link("Tensor") == f"{KNOWLEDGE}/Tensor.md"


def test_ambiguous_stem_prefers_shallowest_path(vault):
    assert vault.resolve_link("Shared") == "Shared.md"


def test_resolves_via_frontmatter_alias(vault):
    assert vault.resolve_link("The Nickname") == f"{INBOX}/Aliased.md"


def test_frontmatter_links_count_as_edges(tmp_path):
    (tmp_path / "A.md").write_text('---\nsource: "[[B]]"\n---\n# A\n', encoding="utf-8")
    (tmp_path / "B.md").write_text("# B\n", encoding="utf-8")
    v = Vault(tmp_path)
    v.refresh()
    assert v.backlinks("B.md") == ["A.md"]


# --- graph ------------------------------------------------------------------


def test_backlinks(vault):
    assert f"{KNOWLEDGE}/Tensor.md" in vault.backlinks("Linear Algebra")
    assert f"{INBOX}/MCMC.md" in vault.backlinks("Linear Algebra")


def test_unresolved_links_are_reported(vault):
    assert vault.unresolved("Linear Algebra") == ["Missing Note"]


def test_neighborhood_depth(vault):
    d1 = vault.neighborhood("Linear Algebra", depth=1)
    assert f"{KNOWLEDGE}/Tensor.md" in d1
    d2 = vault.neighborhood("Linear Algebra", depth=2)
    assert set(d1).issubset(set(d2))


# --- frontmatter ------------------------------------------------------------


def test_frontmatter_round_trip_preserves_key_order(vault):
    note = vault.get("Linear Algebra")
    assert list(note.frontmatter.keys()) == ["title", "tags", "status"]
    rebuilt = join_frontmatter(note.frontmatter, note.body)
    assert rebuilt.index("title:") < rebuilt.index("tags:") < rebuilt.index("status:")


def test_note_without_frontmatter_parses_cleanly(vault):
    note = vault.get("Tensor")
    assert dict(note.frontmatter) == {}
    assert note.body.startswith("# Tensor")


def test_malformed_yaml_does_not_lose_content():
    fm, body = split_frontmatter("---\n: : bad yaml : :\n---\n# Body\n")
    assert "# Body" in body


def test_tags_normalized_from_list_and_string(tmp_path):
    (tmp_path / "A.md").write_text("---\ntags: '#alpha, beta'\n---\n", encoding="utf-8")
    v = Vault(tmp_path)
    v.refresh()
    assert v.get("A").tags == ["alpha", "beta"]


# --- writes and containment -------------------------------------------------


def test_absolute_path_is_rejected(vault):
    """Regression: '/etc/passwd' must error, not silently become 'etc/passwd'."""
    with pytest.raises(VaultError, match="absolute"):
        vault.resolve_path("/etc/passwd.md")


@pytest.mark.parametrize(
    "bad",
    ["../escape.md", "../../etc/passwd.md", f"{INBOX}/../../escape.md", "a/../../../oops.md"],
)
def test_traversal_is_rejected(vault, bad):
    with pytest.raises(VaultError):
        vault.resolve_path(bad)


def test_write_is_atomic_and_leaves_no_temp_file(vault):
    rel = vault.write_atomic(f"{INBOX}/New Note.md", "# New\n\n[[Tensor]]\n")
    assert rel == f"{INBOX}/New Note.md"
    assert not list(vault.root.rglob("*.miradian.tmp"))
    assert f"{INBOX}/New Note.md" in vault.backlinks("Tensor")


def test_write_refuses_non_markdown(vault):
    with pytest.raises(VaultError, match="Only .md"):
        vault.write_atomic(f"{INBOX}/evil.sh", "#!/bin/sh\n")


def test_delete_moves_to_trash_and_is_recoverable(vault):
    dest = vault.trash("Tensor")
    assert dest.startswith(".trash/")
    assert (vault.root / dest).exists()
    assert "Tensor" not in [n.stem for n in vault.notes().values()]


def test_missing_note_error_suggests_next_step(vault):
    with pytest.raises(VaultError, match="vault_search"):
        vault.get("Linear Algbra")


# --- search -----------------------------------------------------------------


def test_search_finds_content_with_context(vault):
    hits = vault.search("Tensor")
    assert any(h["path"] == f"{KNOWLEDGE}/Linear Algebra.md" for h in hits)


def test_search_filters_by_tag_and_frontmatter(vault):
    assert [h["path"] for h in vault.search(tag="math")] == [f"{KNOWLEDGE}/Linear Algebra.md"]
    assert vault.search(frontmatter={"status": "active"})
    assert not vault.search(frontmatter={"status": "archived"})


def test_search_respects_path_glob(vault):
    hits = vault.search("Linear Algebra", path_glob=f"{INBOX}/*")
    assert all(h["path"].startswith(INBOX) for h in hits)


def test_excalidraw_body_excluded_from_search_but_kept_in_graph(tmp_path):
    (tmp_path / "Drawing.md").write_text(
        "---\nexcalidraw-plugin: parsed\n---\n# Drawing\n\nsecretblob [[Target]]\n", encoding="utf-8"
    )
    (tmp_path / "Target.md").write_text("# Target\n", encoding="utf-8")
    v = Vault(tmp_path)
    v.refresh()
    assert v.get("Drawing").is_excalidraw
    assert not [h for h in v.search("secretblob") if h["matches"]]  # body not searched
    assert v.backlinks("Target.md") == ["Drawing.md"]  # but still an edge


def test_index_picks_up_external_changes(vault):
    (vault.root / f"{INBOX}/Later.md").write_text("# Later\n\n[[Tensor]]\n", encoding="utf-8")
    assert f"{INBOX}/Later.md" in vault.backlinks("Tensor")
