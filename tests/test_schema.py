"""Tests for the frontmatter schema layer.

Frontmatter is an index, and an index is only worth having if its vocabulary is
closed. These tests pin down the two ways that guarantee gets lost: a value that
is nearly-but-not-quite an allowed one (`Porject`, `wip`), and a tag that enters
the vocabulary by accident rather than on purpose.
"""

from __future__ import annotations

import pytest

import schema as schema_mod
from schema import Schema, normalize, suggest_tag_name, validate

INBOX = "00 📥 Inbox"
PROJECTS = "03 🛠 Projects"


@pytest.fixture
def sch() -> Schema:
    return Schema(
        auto={"date": "today", "ai_generated": "true"},
        required=["title", "tags", "status", "vault_type", "summary"],
        optional=["source"],
        enums={
            "vault_type": ["knowledge", "idea", "inbox", "log", "project", "personal"],
            "status": ["active", "in-review", "archived", "unprocessed", "n/a"],
        },
        open=["source"],
        constraints={"summary": {"max_length": 250}},
        folders={INBOX: "inbox", PROJECTS: "project", "02 📚 Knowledge": "knowledge"},
        tags=["ai", "machine-learning", "tech", "cooking"],
    )


def good(**over):
    fm = {
        "title": "T",
        "tags": ["ai"],
        "status": "active",
        "vault_type": "idea",
        "summary": "A summary",
    }
    fm.update(over)
    return fm


# --- case folding -----------------------------------------------------------


def test_controlled_fields_are_lowercased(sch):
    fm = good(tags=["AI", "Tech"], status="ACTIVE", vault_type="Knowledge")
    normalize(fm, sch)
    assert fm["tags"] == ["ai", "tech"]
    assert fm["status"] == "active"
    assert fm["vault_type"] == "knowledge"
    assert not validate(fm, "x.md", sch)


def test_prose_fields_keep_their_case(sch):
    fm = good(title="Metropolis Algorithm", summary="A TL;DR", source="Wikipedia")
    normalize(fm, sch)
    assert fm["title"] == "Metropolis Algorithm"
    assert fm["summary"] == "A TL;DR"
    assert fm["source"] == "Wikipedia"


# --- closed vocabularies ----------------------------------------------------


def test_valid_frontmatter_passes(sch):
    assert validate(good(), f"{INBOX}/x.md", sch) == []


def test_unknown_vault_type_is_rejected_with_a_suggestion(sch):
    errs = validate(good(vault_type="porject"), f"{PROJECTS}/x.md", sch)
    assert errs and "not an allowed value" in errs[0]
    assert "'project'" in errs[0]


def test_bad_vault_type_error_names_the_folder_convention(sch):
    """The folder you are writing into usually settles the vault_type."""
    errs = validate(good(vault_type="porject"), f"{PROJECTS}/x.md", sch)
    assert PROJECTS in errs[0] and "holds vault_type" in errs[0]


def test_unknown_status_is_rejected(sch):
    errs = validate(good(status="wip"), "x.md", sch)
    assert errs and "status" in errs[0] and "active" in errs[0]


def test_missing_required_fields_are_all_reported_at_once(sch):
    errs = validate({"title": "T"}, "x.md", sch)
    assert len(errs) == 1
    for field in ("tags", "status", "vault_type", "summary"):
        assert field in errs[0]


def test_empty_value_counts_as_missing(sch):
    errs = validate(good(summary=""), "x.md", sch)
    assert any("summary" in e for e in errs)


# --- the governed tag vocabulary --------------------------------------------


def test_unregistered_tag_is_rejected_and_points_at_the_near_match(sch):
    errs = validate(good(tags=["machinelearning"]), "x.md", sch)
    assert errs
    assert "machine-learning" in errs[0]
    assert "vault_list_tags" in errs[0] and "vault_create_tag" in errs[0]


def test_registered_tag_passes(sch):
    assert validate(good(tags=["machine-learning", "ai"]), "x.md", sch) == []


def test_tag_naming_convention_is_enforced():
    assert suggest_tag_name("machine-learning") is None
    assert "kebab-case" in suggest_tag_name("Machine Learning")
    assert "'machine-learning'" in suggest_tag_name("Machine Learning")
    assert suggest_tag_name("") is not None


def test_close_tags_finds_the_near_duplicate(sch):
    assert "machine-learning" in sch.close_tags("machinelearn")


# --- constraints ------------------------------------------------------------


def test_overlong_summary_is_rejected(sch):
    errs = validate(good(summary="x" * 251), "x.md", sch)
    assert errs and "250" in errs[0] and "index field" in errs[0]


def test_summary_at_the_limit_passes(sch):
    assert validate(good(summary="x" * 250), "x.md", sch) == []


# --- loading from the vault -------------------------------------------------


def test_schema_loads_from_the_vault_note(tmp_path):
    (tmp_path / schema_mod.SCHEMA_NOTE).write_text(
        "---\n"
        "title: Schema\n"
        "schema:\n"
        "  required: [title]\n"
        "  enums:\n"
        "    status: [active, archived]\n"
        "  tags: [ai]\n"
        "---\n# Schema\n",
        encoding="utf-8",
    )
    from vault import Vault

    v = Vault(tmp_path)
    v.refresh()
    sch = schema_mod.load(v)
    assert sch is not None
    assert sch.required == ["title"]
    assert sch.enums["status"] == ["active", "archived"]
    assert sch.tags == ["ai"]


def test_vault_without_a_schema_note_degrades_to_none(tmp_path):
    (tmp_path / "A.md").write_text("# A\n", encoding="utf-8")
    from vault import Vault

    v = Vault(tmp_path)
    v.refresh()
    assert schema_mod.load(v) is None


def test_tag_counts_come_from_the_index_not_a_rescan(tmp_path):
    (tmp_path / "A.md").write_text("---\ntags: [ai, Tech]\n---\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("---\ntags: [ai]\n---\n", encoding="utf-8")
    from vault import Vault

    v = Vault(tmp_path)
    v.refresh()
    counts = v.tag_counts()
    assert counts["ai"] == 2
    assert counts["tech"] == 1  # counted case-insensitively
