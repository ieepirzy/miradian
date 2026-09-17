"""Tests for the generic structured-document layer.

Everything runs against tmp_path. Nothing here may point at a real vault -- the
service writes, and the real vault is a live git repo and a Syncthing replica.

Folder names deliberately mirror the real vault (emoji, spaces, non-ASCII), because
that is what the code will actually be handed.
"""

from __future__ import annotations

import pytest

from documents import (
    AlreadyExists,
    Conflict,
    DocumentError,
    DocumentService,
    Malformed,
    NotFound,
    PolicyRejected,
    plain,
)
from vault import Vault

ROOT = "03 🛠 Projects"
KNOWLEDGE = "02 📚 Knowledge"


def write(base, rel: str, text: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    """A vault with two projects, an empty one, loose files, and a knowledge area."""
    write(
        tmp_path,
        f"{ROOT}/Loimi/plan.md",
        "---\ndoc_type: objective\ntitle: Ship v1\nstatus: open\n---\n# Ship v1\n\nBody text.\n",
    )
    write(
        tmp_path,
        f"{ROOT}/Loimi/task-1.md",
        "---\ndoc_type: work_item\ntitle: Wire the API\n---\nDo the thing.\n",
    )
    # Ordinary project knowledge: no doc_type, must not be treated as project state.
    write(tmp_path, f"{ROOT}/Loimi/research notes.md", "# Notes\n\nJust thinking out loud.\n")
    write(
        tmp_path,
        f"{ROOT}/Muutto365/decision-0001.md",
        "---\ndoc_type: decision\ntitle: Use Postgres\n---\nBecause.\n",
    )
    (tmp_path / ROOT / "Empty Project").mkdir(parents=True)
    # A loose file directly in the root is not a project.
    write(tmp_path, f"{ROOT}/i2pd-hardening.md", "# i2pd\n")
    # Outside every root: must never appear.
    write(
        tmp_path,
        f"{KNOWLEDGE}/Tensor.md",
        "---\ndoc_type: work_item\ntitle: Not project state\n---\nDecoy.\n",
    )
    write(tmp_path, ".obsidian/workspace.json", "{}")

    v = Vault(tmp_path)
    return DocumentService(v, [ROOT])


# --- project discovery -----------------------------------------------------


def test_projects_are_directories_under_the_root(tree):
    ids = [p.id for p in tree.discover_projects()]
    assert ids == [f"{ROOT}/Empty Project", f"{ROOT}/Loimi", f"{ROOT}/Muutto365"]


def test_an_empty_directory_is_already_a_project(tree):
    """Starting a project must be nothing more than mkdir."""
    empty = next(p for p in tree.discover_projects() if p.name == "Empty Project")
    assert empty.document_count == 0
    assert empty.updated_at is None


def test_a_new_directory_becomes_a_project_with_no_other_ceremony(tree, tmp_path):
    (tmp_path / ROOT / "Brand New").mkdir()
    assert f"{ROOT}/Brand New" in [p.id for p in tree.discover_projects()]


def test_loose_files_in_the_root_are_not_projects(tree):
    assert all(p.name != "i2pd-hardening.md" for p in tree.discover_projects())


def test_document_counts_ignore_unstructured_notes(tree):
    loimi = next(p for p in tree.discover_projects() if p.name == "Loimi")
    assert loimi.document_count == 2  # plan + task-1, not "research notes"


def test_documents_outside_the_roots_are_invisible(tree):
    paths = [r.path for r in tree.list_documents(include_unstructured=True)]
    assert not any(p.startswith(KNOWLEDGE) for p in paths)


# --- classification --------------------------------------------------------


def test_only_marked_documents_are_listed_by_default(tree):
    paths = [r.path for r in tree.list_documents(f"{ROOT}/Loimi")]
    assert paths == [f"{ROOT}/Loimi/plan.md", f"{ROOT}/Loimi/task-1.md"]


def test_unstructured_notes_coexist_and_are_listable_on_request(tree):
    paths = [r.path for r in tree.list_documents(f"{ROOT}/Loimi", include_unstructured=True)]
    assert f"{ROOT}/Loimi/research notes.md" in paths
    ref = next(r for r in tree.list_documents(f"{ROOT}/Loimi", include_unstructured=True)
               if r.path.endswith("research notes.md"))
    assert ref.structured is False and ref.doc_type is None


def test_doc_type_filter(tree):
    refs = tree.list_documents(doc_type="decision")
    assert [r.path for r in refs] == [f"{ROOT}/Muutto365/decision-0001.md"]


def test_doc_type_values_are_not_validated(tree):
    """The consumer owns the vocabulary; Miradian only tests for the field."""
    doc = tree.create_document(f"{ROOT}/Loimi/weird.md", metadata={"doc_type": "not-a-real-type"})
    assert doc.doc_type == "not-a-real-type" and doc.structured


# --- identity --------------------------------------------------------------


def test_managed_documents_are_stamped_with_a_uid(tree):
    refs = tree.list_documents()
    assert all(r.id and len(r.id) == 32 for r in refs)


def test_unstructured_notes_are_never_stamped(tree, tmp_path):
    before = (tmp_path / ROOT / "Loimi" / "research notes.md").read_text(encoding="utf-8")
    tree.list_documents(include_unstructured=True)
    after = (tmp_path / ROOT / "Loimi" / "research notes.md").read_text(encoding="utf-8")
    assert before == after


def test_stamping_is_idempotent(tree, tmp_path):
    tree.list_documents()
    first = (tmp_path / ROOT / "Loimi" / "plan.md").read_text(encoding="utf-8")
    tree.list_documents()
    tree.list_documents()
    assert (tmp_path / ROOT / "Loimi" / "plan.md").read_text(encoding="utf-8") == first


def test_stamping_preserves_the_body_and_existing_metadata(tree, tmp_path):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    assert doc.body.strip() == "# Ship v1\n\nBody text.".strip()
    assert doc.metadata["title"] == "Ship v1" and doc.metadata["status"] == "open"


def test_duplicate_uids_are_repaired(tree, tmp_path):
    tree.list_documents()
    plan = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    # Simulate duplicating a note in Obsidian: the copy carries the same uid.
    write(
        tmp_path,
        f"{ROOT}/Loimi/plan copy.md",
        f"---\ndoc_type: objective\ntitle: Copy\nuid: {plan.id}\n---\nCopied.\n",
    )
    refs = tree.list_documents()
    ids = [r.id for r in refs]
    assert len(ids) == len(set(ids))
    assert tree.read_document(path=f"{ROOT}/Loimi/plan.md").id == plan.id


def test_identity_survives_a_move(tree):
    tree.list_documents()
    before = tree.read_document(path=f"{ROOT}/Loimi/task-1.md")
    after = tree.move_document(id=before.id, to=f"{ROOT}/Muutto365/moved.md")
    assert after.id == before.id
    assert after.path == f"{ROOT}/Muutto365/moved.md"
    assert tree.read_document(id=before.id).path == after.path


def test_uid_stamping_can_be_switched_off(tmp_path):
    write(tmp_path, f"{ROOT}/P/x.md", "---\ndoc_type: task\n---\nBody\n")
    svc = DocumentService(Vault(tmp_path), [ROOT], assign_uid=False)
    refs = svc.list_documents()
    assert refs[0].id is None
    assert "uid" not in (tmp_path / ROOT / "P" / "x.md").read_text(encoding="utf-8")


# --- addressing ------------------------------------------------------------


def test_lookup_by_path_is_exact_not_obsidian_style(tree):
    """A bare note name must not resolve: that is an Obsidian rule, not an API one."""
    with pytest.raises(NotFound):
        tree.read_document(path="plan")


def test_unknown_uid_is_not_found(tree):
    with pytest.raises(NotFound):
        tree.read_document(id="0" * 32)


def test_read_requires_an_identifier(tree):
    with pytest.raises(DocumentError):
        tree.read_document()


# --- metadata patching -----------------------------------------------------


def test_patch_sets_and_unsets_without_touching_the_body(tree):
    tree.list_documents()
    before = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    doc, missing = tree.patch_metadata(
        id=before.id, set_fields={"status": "done", "owner": "ilari"}, unset_fields=["title"]
    )
    assert doc.body == before.body
    assert doc.metadata["status"] == "done" and doc.metadata["owner"] == "ilari"
    assert "title" not in doc.metadata
    assert missing == []


def test_patch_reports_fields_that_were_not_there(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    _, missing = tree.patch_metadata(id=doc.id, unset_fields=["nope", "status"])
    assert missing == ["nope"]


def test_patch_preserves_key_order_and_untouched_fields(tree, tmp_path):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    tree.patch_metadata(id=doc.id, set_fields={"status": "done"})
    text = (tmp_path / ROOT / "Loimi" / "plan.md").read_text(encoding="utf-8")
    keys = [line.split(":")[0] for line in text.split("---")[1].strip().split("\n")]
    assert keys[:3] == ["doc_type", "title", "status"]
    assert "Body text." in text


def test_patch_preserves_yaml_comments_and_quoting(tmp_path):
    write(
        tmp_path,
        f"{ROOT}/P/x.md",
        '---\ndoc_type: task\n# why this exists\ntitle: "Quoted Title"\n---\nBody\n',
    )
    svc = DocumentService(Vault(tmp_path), [ROOT])
    doc = svc.list_documents()[0]
    svc.patch_metadata(id=doc.id, set_fields={"status": "open"})
    text = (tmp_path / ROOT / "P" / "x.md").read_text(encoding="utf-8")
    assert "# why this exists" in text
    assert '"Quoted Title"' in text


def test_patch_refuses_a_document_with_broken_frontmatter(tmp_path):
    """Rewriting it would prepend a second frontmatter block above the broken one."""
    write(tmp_path, f"{ROOT}/P/broken.md", "---\ndoc_type: task\n  bad: [unclosed\n---\nBody\n")
    svc = DocumentService(Vault(tmp_path), [ROOT], assign_uid=False)
    with pytest.raises(Malformed):
        svc.patch_metadata(path=f"{ROOT}/P/broken.md", set_fields={"status": "open"})
    assert (tmp_path / ROOT / "P" / "broken.md").read_text(encoding="utf-8").count("---") == 2


def test_broken_frontmatter_does_not_break_a_listing(tmp_path):
    write(tmp_path, f"{ROOT}/P/broken.md", "---\ndoc_type: task\n  bad: [unclosed\n---\nBody\n")
    write(tmp_path, f"{ROOT}/P/fine.md", "---\ndoc_type: task\n---\nBody\n")
    svc = DocumentService(Vault(tmp_path), [ROOT])
    paths = [r.path for r in svc.list_documents()]
    assert f"{ROOT}/P/fine.md" in paths  # the healthy one still converges and lists


def test_patch_cannot_unset_the_identity(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    with pytest.raises(DocumentError):
        tree.patch_metadata(id=doc.id, unset_fields=["uid"])


def test_patch_needs_something_to_do(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    with pytest.raises(DocumentError):
        tree.patch_metadata(id=doc.id)


def test_patch_runs_a_supplied_policy(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    with pytest.raises(PolicyRejected) as e:
        tree.patch_metadata(
            id=doc.id,
            set_fields={"status": "banana"},
            policy=lambda fm, path: ["status is not allowed to be banana"],
        )
    assert e.value.errors == ["status is not allowed to be banana"]


def test_a_rejected_policy_leaves_the_file_alone(tree, tmp_path):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    before = (tmp_path / ROOT / "Loimi" / "plan.md").read_text(encoding="utf-8")
    with pytest.raises(PolicyRejected):
        tree.patch_metadata(id=doc.id, set_fields={"status": "x"}, policy=lambda fm, p: ["no"])
    assert (tmp_path / ROOT / "Loimi" / "plan.md").read_text(encoding="utf-8") == before


def test_writing_in_a_doc_type_makes_a_note_managed_and_stamps_it(tree):
    """Promoting a note must return its identity, whichever verb did the promoting."""
    doc = tree.write_document(
        path=f"{ROOT}/Loimi/research notes.md",
        body="Now it is project state.",
        metadata={"doc_type": "decision", "title": "Promoted"},
    )
    assert doc.structured and doc.id
    assert tree.read_document(id=doc.id).path == f"{ROOT}/Loimi/research notes.md"


def test_patching_in_a_doc_type_makes_a_note_managed_and_stamps_it(tree):
    ref = tree.patch_metadata(
        path=f"{ROOT}/Loimi/research notes.md", set_fields={"doc_type": "decision"}
    )[0]
    assert ref.structured and ref.doc_type == "decision" and ref.id


# --- writes ----------------------------------------------------------------


def test_create_assigns_an_identity_in_the_same_round_trip(tree):
    doc = tree.create_document(
        f"{ROOT}/Loimi/new-task.md", body="Do it.", metadata={"doc_type": "work_item", "title": "New"}
    )
    assert doc.id and doc.path == f"{ROOT}/Loimi/new-task.md"
    assert tree.read_document(id=doc.id).body.strip() == "Do it."


def test_create_refuses_to_clobber(tree):
    with pytest.raises(AlreadyExists):
        tree.create_document(f"{ROOT}/Loimi/plan.md", body="x")


def test_write_without_metadata_keeps_the_existing_frontmatter(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    updated = tree.write_document(id=doc.id, body="Completely new prose.")
    assert updated.metadata["title"] == "Ship v1"
    assert updated.body.strip() == "Completely new prose."


def test_write_with_metadata_replaces_it_but_keeps_the_uid(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    updated = tree.write_document(id=doc.id, body="x", metadata={"doc_type": "objective"})
    assert updated.id == doc.id
    assert "title" not in updated.metadata


def test_version_changes_when_the_document_changes(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    updated = tree.write_document(id=doc.id, body="different")
    assert updated.version != doc.version


def test_expected_version_detects_a_conflict(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    tree.write_document(id=doc.id, body="someone else got here first")
    with pytest.raises(Conflict) as e:
        tree.write_document(id=doc.id, body="my edit", expected_version=doc.version)
    assert e.value.current_version != doc.version


def test_a_matching_expected_version_is_accepted(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")
    updated = tree.write_document(id=doc.id, body="mine", expected_version=doc.version)
    assert updated.body.strip() == "mine"


def test_move_refuses_to_overwrite(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/task-1.md")
    with pytest.raises(DocumentError):
        tree.move_document(id=doc.id, to=f"{ROOT}/Loimi/plan.md")


def test_delete_removes_the_document_from_listings(tree):
    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/task-1.md")
    result = tree.delete_document(id=doc.id)
    assert result["deleted"] and result["id"] == doc.id
    assert doc.path not in [r.path for r in tree.list_documents()]


# --- scope and containment -------------------------------------------------


def test_path_scope_check(tree):
    assert tree.path_in_scope(f"{ROOT}/Loimi/plan.md")
    assert not tree.path_in_scope(f"{KNOWLEDGE}/Tensor.md")


def test_traversal_is_still_refused(tree):
    with pytest.raises(DocumentError):
        tree.create_document("../escaped.md", body="x")


def test_ignored_directories_stay_ignored(tmp_path):
    write(tmp_path, f"{ROOT}/P/ok.md", "---\ndoc_type: task\n---\nok\n")
    write(tmp_path, f"{ROOT}/P/.trash/gone.md", "---\ndoc_type: task\n---\ngone\n")
    write(tmp_path, f"{ROOT}/P/x.sync-conflict-20260101.md", "---\ndoc_type: task\n---\nconflict\n")
    write(tmp_path, f"{ROOT}/P/.stversions/old.md", "---\ndoc_type: task\n---\nold\n")
    svc = DocumentService(Vault(tmp_path), [ROOT])
    assert [r.path for r in svc.list_documents()] == [f"{ROOT}/P/ok.md"]


# --- folders and serialisation ---------------------------------------------


def test_list_folders_returns_project_subdirectories(tmp_path):
    write(tmp_path, f"{ROOT}/P/specs/a.md", "---\ndoc_type: task\n---\na\n")
    svc = DocumentService(Vault(tmp_path), [ROOT])
    assert svc.list_folders(f"{ROOT}/P") == [f"{ROOT}/P/specs"]


def test_metadata_is_json_serialisable(tmp_path):
    """Unquoted YAML dates come back as date objects, which json.dumps refuses."""
    import json

    write(tmp_path, f"{ROOT}/P/x.md", "---\ndoc_type: task\ndate: 2026-04-07\n---\nBody\n")
    svc = DocumentService(Vault(tmp_path), [ROOT])
    ref = svc.list_documents()[0]
    assert json.dumps(ref.as_dict())
    assert ref.metadata["date"] == "2026-04-07"


def test_plain_handles_nested_structures():
    from datetime import date

    assert plain({"a": [date(2026, 1, 2), {"b": 1}]}) == {"a": ["2026-01-02", {"b": 1}]}


# --- concurrency -----------------------------------------------------------
#
# The vault has three writers: a human in Obsidian, agents over MCP, and a
# service over HTTP. These cover the two that go through this layer. They are
# the first concurrency cases in either this suite or MiraDesign's.


def test_concurrent_patches_all_land_and_nothing_raises(tree):
    """Eight threads, eight fields, one document.

    A smoke test, and worth being precise about what it does not show: it passes
    with or without the lock, because unlocked callers mutate one shared ruamel
    CommentedMap and so accidentally accumulate each other's fields. The case
    below is the one that actually distinguishes them.
    """
    import threading

    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")

    errors: list[Exception] = []

    def patch(i: int) -> None:
        try:
            tree.patch_metadata(id=doc.id, set_fields={f"field_{i}": i})
        except Exception as e:  # noqa: BLE001 - the assertion is that there are none
            errors.append(e)

    threads = [threading.Thread(target=patch, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    final = tree.read_document(id=doc.id)
    assert all(final.metadata.get(f"field_{i}") == i for i in range(8)), final.metadata
    # The body and the pre-existing metadata survived all eight.
    assert final.metadata["title"] == "Ship v1"
    assert "Body text." in final.body


def test_concurrent_writes_leave_a_readable_document(tree):
    """Last-writer-wins on the body is expected; corruption is not."""
    import threading

    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/task-1.md")

    def write(i: int) -> None:
        tree.write_document(id=doc.id, body=f"body from writer {i}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = tree.read_document(id=doc.id)
    assert final.body.strip().startswith("body from writer")
    assert final.id == doc.id           # identity survived every one of them
    assert final.doc_type == "work_item"


def test_two_patches_from_one_read_cannot_both_win(tree):
    """Both threads hold the same `expected_version`: exactly one may win.

    This pins the guarantee a consumer actually relies on. It is worth recording
    what it does *not* do, since the temptation is to read it as proof that the
    version check belongs inside the lock: it passes with the check outside too.
    `resolve()` goes through the vault index, which takes the same lock a write
    holds, so a second caller cannot complete its read while the first is
    writing, and the outer check sees the new version anyway.

    The check still belongs inside the transform, but for a reason a test cannot
    show: outside, correctness leans on an incidental property of `resolve()`
    rather than on anything stated. Inside, the version is compared against the
    very bytes the write is built from, and stays right if the index is ever
    cached or made lock-free.
    """
    import threading

    tree.list_documents()
    doc = tree.read_document(path=f"{ROOT}/Loimi/plan.md")

    results: list[str] = []
    barrier = threading.Barrier(2)

    def patch(i: int) -> None:
        barrier.wait()  # maximise the overlap rather than hoping for it
        try:
            tree.patch_metadata(
                id=doc.id, set_fields={"winner": i}, expected_version=doc.version
            )
            results.append("ok")
        except Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=patch, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["conflict", "ok"], results
