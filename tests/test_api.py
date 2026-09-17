"""Tests for the HTTP/JSON surface.

These are about the boundary rather than the mechanics -- auth, scope, status codes
and JSON shape. What happens to the files is covered in test_documents.py.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from api import ApiDispatcher, create_api, parse_tokens
from changes import ChangeJournal
from documents import DocumentService
from vault import Vault

ROOT = "03 🛠 Projects"
KNOWLEDGE = "02 📚 Knowledge"
TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OUTSIDE_UID = "a" * 32


def write(base, rel: str, text: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# The real vault's schema, in miniature. It has to be here for the
# schema-does-not-apply test to mean anything: with no METADATA.md, schema loading
# returns None and that test passes whether or not the API enforces a schema.
METADATA = """---
title: Vault frontmatter schema
schema:
  required: [title, tags, status, vault_type, summary]
  enums:
    status: [active, in-review, archived]
    vault_type: [knowledge, project, idea]
  tags: [ai, coding]
  folders: {}
---
# Schema
"""


@pytest.fixture
def client(tmp_path):
    vault_dir = tmp_path / "vault"
    # At the vault root, outside PROJECT_ROOTS, so it never shows up in listings.
    write(vault_dir, "METADATA.md", METADATA)
    write(vault_dir, f"{ROOT}/Loimi/plan.md", "---\ndoc_type: objective\ntitle: Plan\n---\n# Plan\n")
    write(vault_dir, f"{ROOT}/Loimi/notes.md", "# Ordinary notes\n")
    write(vault_dir, f"{KNOWLEDGE}/Tensor.md", "---\ndoc_type: work_item\n---\nDecoy\n")
    # A note outside the roots that already carries a uid -- a template, an import,
    # something a plugin wrote. It is addressable by id, so it is the probe for
    # whether the scope guard actually holds on the by-id path.
    write(
        vault_dir,
        f"{KNOWLEDGE}/Private.md",
        f"---\ndoc_type: work_item\nuid: {OUTSIDE_UID}\n---\nPrivate knowledge.\n",
    )
    service = DocumentService(Vault(vault_dir), [ROOT])
    journal = ChangeJournal(service, tmp_path / "state")
    app = create_api(service, journal, tokens={"miradesign": TOKEN}, capabilities={"links": False})
    c = TestClient(app)
    c.vault_dir = vault_dir
    c.service = service
    return c


# --- auth ------------------------------------------------------------------


def test_health_needs_no_token(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_everything_else_needs_a_token(client):
    r = client.get("/api/v1/projects")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_a_wrong_token_is_refused(client):
    assert client.get("/api/v1/projects", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_valid_token_is_accepted(client):
    assert client.get("/api/v1/projects", headers=AUTH).status_code == 200


def test_with_no_tokens_configured_the_api_refuses_everything(tmp_path):
    """Fail closed: an upgrade must not silently expose the vault."""
    vault_dir = tmp_path / "vault"
    (vault_dir / ROOT).mkdir(parents=True)
    service = DocumentService(Vault(vault_dir), [ROOT])
    app = create_api(service, ChangeJournal(service, tmp_path / "state"), tokens={})
    c = TestClient(app)
    assert c.get("/api/v1/projects").status_code == 503
    assert c.get("/api/v1/health").status_code == 200


def test_an_oauth_token_is_accepted_too(tmp_path):
    vault_dir = tmp_path / "vault"
    (vault_dir / ROOT).mkdir(parents=True)
    service = DocumentService(Vault(vault_dir), [ROOT])
    app = create_api(
        service,
        ChangeJournal(service, tmp_path / "state"),
        tokens={},
        verify_oauth=lambda t: t == "oauth-issued",
    )
    c = TestClient(app)
    assert c.get("/api/v1/projects", headers={"Authorization": "Bearer oauth-issued"}).status_code == 200
    assert c.get("/api/v1/projects", headers={"Authorization": "Bearer other"}).status_code == 401


def test_token_parsing():
    assert parse_tokens("a:1,b:2") == {"a": "1", "b": "2"}
    assert parse_tokens("") == {}
    assert parse_tokens("nocolon,b:2") == {"b": "2"}


# --- scope -----------------------------------------------------------------


def test_paths_outside_the_project_roots_are_refused(client):
    r = client.get("/api/v1/documents/get", params={"path": f"{KNOWLEDGE}/Tensor.md"}, headers=AUTH)
    assert r.status_code == 403 and r.json()["error"] == "out_of_scope"


def test_a_write_cannot_escape_the_roots(client):
    r = client.post(
        "/api/v1/documents/create",
        json={"path": f"{KNOWLEDGE}/sneaky.md", "metadata": {"doc_type": "task"}},
        headers=AUTH,
    )
    assert r.status_code == 403
    assert not (client.vault_dir / KNOWLEDGE / "sneaky.md").exists()


def test_a_move_cannot_escape_the_roots(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    r = client.post(
        "/api/v1/documents/move",
        json={"id": doc["id"], "to": f"{KNOWLEDGE}/escaped.md"},
        headers=AUTH,
    )
    assert r.status_code == 403


# A uid lookup searches the whole vault, so every verb has to be probed by id as
# well as by path. Guarding only the supplied `path` left these reachable, with the
# check landing after the file had already been written.


def test_an_out_of_scope_document_is_not_readable_by_id(client):
    r = client.get("/api/v1/documents/get", params={"id": OUTSIDE_UID}, headers=AUTH)
    assert r.status_code == 403 and r.json()["error"] == "out_of_scope"


def test_an_out_of_scope_document_is_not_writable_by_id(client):
    before = (client.vault_dir / KNOWLEDGE / "Private.md").read_text(encoding="utf-8")
    r = client.post(
        "/api/v1/documents/write", json={"id": OUTSIDE_UID, "body": "clobbered"}, headers=AUTH
    )
    assert r.status_code == 403
    assert (client.vault_dir / KNOWLEDGE / "Private.md").read_text(encoding="utf-8") == before


def test_an_out_of_scope_document_is_not_patchable_by_id(client):
    before = (client.vault_dir / KNOWLEDGE / "Private.md").read_text(encoding="utf-8")
    r = client.post(
        "/api/v1/documents/patch-metadata",
        json={"id": OUTSIDE_UID, "set": {"status": "pwned"}},
        headers=AUTH,
    )
    assert r.status_code == 403
    assert (client.vault_dir / KNOWLEDGE / "Private.md").read_text(encoding="utf-8") == before


def test_an_out_of_scope_document_is_not_deletable_by_id(client):
    r = client.post("/api/v1/documents/delete", json={"id": OUTSIDE_UID}, headers=AUTH)
    assert r.status_code == 403
    assert (client.vault_dir / KNOWLEDGE / "Private.md").exists()


def test_an_out_of_scope_document_cannot_be_moved_into_the_roots(client):
    """The destination being in scope must not launder an out-of-scope source."""
    r = client.post(
        "/api/v1/documents/move",
        json={"id": OUTSIDE_UID, "to": f"{ROOT}/Loimi/stolen.md"},
        headers=AUTH,
    )
    assert r.status_code == 403
    assert not (client.vault_dir / ROOT / "Loimi" / "stolen.md").exists()
    assert (client.vault_dir / KNOWLEDGE / "Private.md").exists()


# --- shape -----------------------------------------------------------------


def test_root_advertises_capabilities(client):
    body = client.get("/api/v1", headers=AUTH).json()
    assert body["api_version"] == "v1"
    assert body["capabilities"] == {"links": False}
    assert body["project_roots"] == [ROOT]
    assert body["doc_type_field"] == "doc_type"


def test_projects_lists_directories(client):
    projects = client.get("/api/v1/projects", headers=AUTH).json()["projects"]
    assert [p["name"] for p in projects] == ["Loimi"]
    assert projects[0]["document_count"] == 1


def test_documents_excludes_unstructured_by_default(client):
    body = client.get("/api/v1/documents", headers=AUTH).json()
    assert [d["path"] for d in body["documents"]] == [f"{ROOT}/Loimi/plan.md"]
    assert body["total"] == 1


def test_documents_can_include_unstructured(client):
    body = client.get(
        "/api/v1/documents", params={"include_unstructured": "true"}, headers=AUTH
    ).json()
    assert len(body["documents"]) == 2


def test_id_and_path_are_separate_fields(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    assert len(doc["id"]) == 32
    assert doc["path"] == f"{ROOT}/Loimi/plan.md"
    assert doc["id"] not in doc["path"]


def test_get_by_id_and_by_path_agree(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    by_id = client.get("/api/v1/documents/get", params={"id": doc["id"]}, headers=AUTH).json()
    by_path = client.get("/api/v1/documents/get", params={"path": doc["path"]}, headers=AUTH).json()
    assert by_id == by_path
    assert "# Plan" in by_id["document"]["body"]


def test_unknown_document_is_404(client):
    r = client.get("/api/v1/documents/get", params={"id": "0" * 32}, headers=AUTH)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# --- mutations -------------------------------------------------------------


def test_create_returns_201_and_an_id(client):
    r = client.post(
        "/api/v1/documents/create",
        json={
            "path": f"{ROOT}/Loimi/task-9.md",
            "body": "Do it.",
            "metadata": {"doc_type": "work_item", "title": "Nine"},
        },
        headers=AUTH,
    )
    assert r.status_code == 201
    assert r.json()["document"]["id"]


def test_create_on_an_existing_path_is_409(client):
    r = client.post("/api/v1/documents/create", json={"path": f"{ROOT}/Loimi/plan.md"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["error"] == "already_exists"


def test_patch_metadata_leaves_the_body_alone(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    r = client.post(
        "/api/v1/documents/patch-metadata",
        json={"id": doc["id"], "set": {"status": "done"}, "unset": ["absent"], "overwrite": True},
        headers=AUTH,
    )
    body = r.json()
    assert body["document"]["metadata"]["status"] == "done"
    assert body["document"]["metadata"]["title"] == "Plan"
    assert "# Plan" in body["document"]["body"]
    assert body["not_present"] == ["absent"]


# An update must name the version it expects or declare it means to clobber.
# "Pass expected_version or you get last-writer-wins" is a convention, and a
# convention fails for the caller who forgot -- who is the caller least likely to
# notice. These pin that the refusal happens before anything is written.


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("write", {"body": "blind"}),
        ("patch-metadata", {"set": {"status": "blind"}}),
        ("move", {"to": f"{ROOT}/Loimi/blind.md"}),
        ("delete", {}),
    ],
)
def test_an_update_without_a_version_or_overwrite_is_refused(client, endpoint, payload):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    before = (client.vault_dir / ROOT / "Loimi" / "plan.md").read_text(encoding="utf-8")

    r = client.post(f"/api/v1/documents/{endpoint}", json={"id": doc["id"], **payload}, headers=AUTH)

    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert (client.vault_dir / ROOT / "Loimi" / "plan.md").read_text(encoding="utf-8") == before
    assert not (client.vault_dir / ROOT / "Loimi" / "blind.md").exists()


def test_overwrite_true_is_the_explicit_way_to_clobber(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    r = client.post(
        "/api/v1/documents/write",
        json={"id": doc["id"], "body": "deliberate", "overwrite": True},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["document"]["body"].strip() == "deliberate"


def test_overwrite_must_be_true_not_merely_present(client):
    """`overwrite: false` is a caller saying no, not a caller saying nothing."""
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    r = client.post(
        "/api/v1/documents/write",
        json={"id": doc["id"], "body": "x", "overwrite": False},
        headers=AUTH,
    )
    assert r.status_code == 400


def test_create_needs_no_version_because_there_is_no_prior_state(client):
    r = client.post(
        "/api/v1/documents/create",
        json={"path": f"{ROOT}/Loimi/fresh.md", "metadata": {"doc_type": "task"}},
        headers=AUTH,
    )
    assert r.status_code == 201


def test_a_stale_expected_version_is_409_with_the_current_one(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    client.post(
        "/api/v1/documents/write",
        json={"id": doc["id"], "body": "theirs", "overwrite": True},
        headers=AUTH,
    )
    r = client.post(
        "/api/v1/documents/write",
        json={"id": doc["id"], "body": "mine", "expected_version": doc["version"]},
        headers=AUTH,
    )
    assert r.status_code == 409 and r.json()["error"] == "version_conflict"
    assert r.json()["current_version"] != doc["version"]


def test_move_keeps_the_id(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    r = client.post(
        "/api/v1/documents/move",
        json={"id": doc["id"], "to": f"{ROOT}/Loimi/renamed.md", "overwrite": True},
        headers=AUTH,
    )
    assert r.json()["document"]["id"] == doc["id"]
    assert r.json()["document"]["path"] == f"{ROOT}/Loimi/renamed.md"


def test_delete(client):
    doc = client.get("/api/v1/documents", headers=AUTH).json()["documents"][0]
    r = client.post(
        "/api/v1/documents/delete", json={"id": doc["id"], "overwrite": True}, headers=AUTH
    )
    assert r.json()["deleted"]
    assert client.get("/api/v1/documents", headers=AUTH).json()["total"] == 0


def test_a_non_json_body_is_a_400_not_a_500(client):
    r = client.post("/api/v1/documents/create", content=b"not json", headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_the_vault_schema_does_not_apply_to_this_surface(client):
    """§4.1 of the design, and the load-bearing decision of the whole change.

    The fixture vault has a METADATA.md requiring five fields with closed
    vocabularies. This write breaks every one of them: no title/status/summary, an
    unregistered tag, a vault_type outside the enum. It must still succeed, because
    a service consumer has no human to approve widening any of them -- and on the
    MCP surface those approvals are gated behind elicitation, which has no HTTP
    analogue. Wire schema validation into the service layer and this test fails,
    which is the point.
    """
    r = client.post(
        "/api/v1/documents/create",
        json={
            "path": f"{ROOT}/Loimi/unvalidated.md",
            "metadata": {
                "doc_type": "brand-new-type",
                "tags": ["a-tag-nobody-registered"],
                "vault_type": "not-in-the-enum",
            },
        },
        headers=AUTH,
    )
    assert r.status_code == 201, r.json()


def test_the_schema_note_is_loadable_so_the_test_above_is_not_vacuous(client):
    """Guards the guard: if METADATA.md stopped parsing, the test above would pass
    for the wrong reason."""
    import schema as schema_mod

    sch = schema_mod.load(client.service.vault)
    assert sch is not None
    assert "title" in sch.required and "vault_type" in sch.enums


# --- changes ---------------------------------------------------------------


def test_changes_round_trip(client):
    first = client.get("/api/v1/changes", headers=AUTH).json()
    assert first["reset"] is True and len(first["changes"]) == 1

    client.post(
        "/api/v1/documents/create",
        json={"path": f"{ROOT}/Loimi/new.md", "metadata": {"doc_type": "task"}},
        headers=AUTH,
    )
    second = client.get("/api/v1/changes", params={"cursor": first["cursor"]}, headers=AUTH).json()
    assert [c["type"] for c in second["changes"]] == ["created"]
    assert second["reset"] is False


# --- dispatcher ------------------------------------------------------------


def test_dispatcher_splits_api_traffic_from_everything_else(client, tmp_path):
    seen = []

    async def other_app(scope, receive, send):
        seen.append(scope.get("path"))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    dispatcher = ApiDispatcher(other_app, client.app)
    c = TestClient(dispatcher)

    assert c.get("/api/v1/health").json() == {"status": "ok"}
    assert seen == []
    assert c.get("/mcp").status_code == 204
    assert seen == ["/mcp"]


def test_dispatcher_sends_lifespan_to_the_mcp_app(client):
    started = []

    async def other_app(scope, receive, send):
        if scope["type"] == "lifespan":
            started.append(True)
            message = await receive()
            assert message["type"] == "lifespan.startup"
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with TestClient(ApiDispatcher(other_app, client.app)):
        pass
    assert started == [True]
