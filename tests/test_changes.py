"""Tests for poll-based change detection.

The contract a consumer relies on: every change to a managed document is reported
exactly once, cursors move forward, and losing the journal is survivable because it
degrades to `reset` plus a full resync rather than to silent data loss.
"""

from __future__ import annotations

import json

import pytest

from changes import ChangeJournal
from documents import DocumentService
from vault import Vault

ROOT = "03 🛠 Projects"


def write(base, rel: str, text: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def doc(title: str, doc_type: str = "work_item") -> str:
    return f"---\ndoc_type: {doc_type}\ntitle: {title}\n---\n# {title}\n"


def edit_externally(base, rel: str, text: str) -> None:
    """Append to a document's body the way Obsidian would: frontmatter untouched.

    Rewriting the whole file from the fixture template would strip the uid Miradian
    stamped, which is a genuinely different event (see the identity-loss test) and
    not what "the human typed a sentence" looks like.
    """
    p = base / rel
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")


@pytest.fixture
def journal(tmp_path):
    vault_dir = tmp_path / "vault"
    state_dir = tmp_path / "state"
    (vault_dir / ROOT / "Loimi").mkdir(parents=True)
    service = DocumentService(Vault(vault_dir), [ROOT])
    j = ChangeJournal(service, state_dir)
    j.vault_dir = vault_dir  # convenience for the tests
    return j


def types(result) -> list[str]:
    return [c["type"] for c in result["changes"]]


# --- the basic cycle -------------------------------------------------------


def test_first_poll_resets_and_returns_everything(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    result = journal.poll()
    assert result["reset"] is True
    assert types(result) == ["created"]


def test_an_unchanged_vault_reports_nothing(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    cursor = journal.poll()["cursor"]
    again = journal.poll(cursor)
    assert again["changes"] == [] and again["reset"] is False
    assert again["cursor"] == cursor


def test_create_update_move_delete_each_reported_once(journal):
    cursor = journal.poll()["cursor"]

    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    r = journal.poll(cursor)
    assert types(r) == ["created"]
    uid, cursor = r["changes"][0]["id"], r["cursor"]

    edit_externally(journal.vault_dir, f"{ROOT}/Loimi/a.md", "\nmore text\n")
    r = journal.poll(cursor)
    assert types(r) == ["updated"] and r["changes"][0]["id"] == uid
    cursor = r["cursor"]

    journal.service.move_document(id=uid, to=f"{ROOT}/Loimi/b.md")
    r = journal.poll(cursor)
    assert types(r) == ["moved"]
    assert r["changes"][0]["previous_path"] == f"{ROOT}/Loimi/a.md"
    assert r["changes"][0]["path"] == f"{ROOT}/Loimi/b.md"
    assert r["changes"][0]["id"] == uid  # identity is untouched by a move
    cursor = r["cursor"]

    journal.service.delete_document(id=uid)
    r = journal.poll(cursor)
    assert types(r) == ["deleted"] and r["changes"][0]["id"] == uid
    assert journal.poll(r["cursor"])["changes"] == []


def test_repeated_edits_collapse_to_one_entry(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    cursor = journal.poll()["cursor"]
    for i in range(3):
        edit_externally(journal.vault_dir, f"{ROOT}/Loimi/a.md", f"\nedit {i}\n")
        journal.poll(cursor)  # each poll scans, but the consumer never advances
    r = journal.poll(cursor)
    assert types(r) == ["updated"]  # one entry, not three


def test_stripping_the_uid_by_hand_reads_as_delete_and_create(journal):
    """A documented consequence of identity living in the file the human edits."""
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    cursor = journal.poll()["cursor"]
    old_uid = journal.service.list_documents()[0].id

    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))  # uid line gone
    r = journal.poll(cursor)

    assert sorted(types(r)) == ["created", "deleted"]
    assert {c["id"] for c in r["changes"] if c["type"] == "deleted"} == {old_uid}


def test_a_move_that_also_changes_content_is_one_move_event(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    cursor = journal.poll()["cursor"]
    uid = journal.service.list_documents()[0].id
    journal.service.write_document(id=uid, body="rewritten")
    journal.service.move_document(id=uid, to=f"{ROOT}/Loimi/moved.md")
    r = journal.poll(cursor)
    assert types(r) == ["moved"]
    assert r["changes"][0]["path"] == f"{ROOT}/Loimi/moved.md"


def test_cursor_is_monotonic(journal):
    cursors = [journal.poll()["cursor"]]
    for i in range(3):
        write(journal.vault_dir, f"{ROOT}/Loimi/{i}.md", doc(str(i)))
        cursors.append(journal.poll(cursors[-1])["cursor"])
    seqs = [int(c.split(":")[1]) for c in cursors]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


# --- what belongs in the feed ---------------------------------------------


def test_unstructured_notes_never_enter_the_feed(journal):
    cursor = journal.poll()["cursor"]
    write(journal.vault_dir, f"{ROOT}/Loimi/notes.md", "# Just a note\n")
    assert journal.poll(cursor)["changes"] == []


def test_gaining_the_marker_reads_as_created(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/notes.md", "---\ntitle: Notes\n---\n# Notes\n")
    cursor = journal.poll()["cursor"]
    journal.service.patch_metadata(path=f"{ROOT}/Loimi/notes.md", set_fields={"doc_type": "decision"})
    assert types(journal.poll(cursor)) == ["created"]


def test_losing_the_marker_reads_as_deleted(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    cursor = journal.poll()["cursor"]
    uid = journal.service.list_documents()[0].id
    journal.service.patch_metadata(id=uid, unset_fields=["doc_type"])
    assert types(journal.poll(cursor)) == ["deleted"]


def test_documents_outside_the_roots_are_not_watched(journal):
    cursor = journal.poll()["cursor"]
    write(journal.vault_dir, "02 📚 Knowledge/x.md", doc("Decoy"))
    assert journal.poll(cursor)["changes"] == []


# --- recovery --------------------------------------------------------------


def test_a_stale_epoch_forces_a_full_resync(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    write(journal.vault_dir, f"{ROOT}/Loimi/b.md", doc("B"))
    journal.poll()
    r = journal.poll("someoneelsesepoch:3")
    assert r["reset"] is True and len(r["changes"]) == 2
    assert types(r) == ["created", "created"]


def test_a_cursor_from_the_future_forces_a_resync(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    journal.poll()
    assert journal.poll(f"{journal.epoch}:99999")["reset"] is True


def test_a_malformed_cursor_forces_a_resync(journal):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    journal.poll()
    assert journal.poll("not-a-cursor")["reset"] is True


def test_state_survives_a_restart(journal, tmp_path):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    cursor = journal.poll()["cursor"]
    restarted = ChangeJournal(journal.service, tmp_path / "state")
    assert restarted.poll(cursor)["reset"] is False
    assert restarted.poll(cursor)["changes"] == []


def test_a_lost_journal_degrades_to_a_resync_not_an_error(journal, tmp_path):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    cursor = journal.poll()["cursor"]
    (tmp_path / "state" / "changes.json").unlink()
    restarted = ChangeJournal(journal.service, tmp_path / "state")
    r = restarted.poll(cursor)
    assert r["reset"] is True and len(r["changes"]) == 1


def test_a_corrupt_journal_degrades_to_a_resync_not_a_crash(journal, tmp_path):
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    journal.poll()
    (tmp_path / "state" / "changes.json").write_text("{not json", encoding="utf-8")
    restarted = ChangeJournal(journal.service, tmp_path / "state")
    assert restarted.poll("anything:1")["reset"] is True


def test_the_journal_refuses_to_live_inside_the_vault(tmp_path):
    vault_dir = tmp_path / "vault"
    (vault_dir / ROOT).mkdir(parents=True)
    service = DocumentService(Vault(vault_dir), [ROOT])
    with pytest.raises(ValueError, match="inside the vault"):
        ChangeJournal(service, vault_dir / ".miradian")


def test_journal_state_is_readable_json(journal, tmp_path):
    """Boring and inspectable: a human debugging this can just read the file."""
    write(journal.vault_dir, f"{ROOT}/Loimi/a.md", doc("A"))
    journal.poll()
    state = json.loads((tmp_path / "state" / "changes.json").read_text(encoding="utf-8"))
    assert state["seq"] == 1 and len(state["entries"]) == 1


# --- paging ----------------------------------------------------------------


def test_changes_page_and_the_cursor_carries_on(journal):
    for i in range(5):
        write(journal.vault_dir, f"{ROOT}/Loimi/{i}.md", doc(str(i)))
    first = journal.poll(limit=2)
    assert len(first["changes"]) == 2 and first["has_more"] is True
    second = journal.poll(first["cursor"], limit=10)
    assert len(second["changes"]) == 3 and second["has_more"] is False
    assert journal.poll(second["cursor"])["changes"] == []


def test_tombstones_are_capped(journal):
    j = ChangeJournal(journal.service, journal.state_dir, tombstone_limit=1)
    for i in range(3):
        write(journal.vault_dir, f"{ROOT}/Loimi/{i}.md", doc(str(i)))
    j.poll()
    for ref in j.service.list_documents():
        j.service.delete_document(id=ref.id)
    j.poll()
    entries = json.loads((j.state_dir / "changes.json").read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 1
