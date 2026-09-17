"""Tests for the human-approval gate on schema changes.

Widening the vocabulary (a new tag, folder, or enum value) is permanent, and a
vocabulary that grows by accident is the exact drift the schema exists to stop.
So it needs a human, and the gate has to hold for an agent running unattended --
which is precisely when nobody is watching it fail.

The gate is MCP elicitation, which fails closed: a client with no human attached
cannot support it, so the request errors and the change is refused. These tests
pin that down, including the case that matters most (no human, no approval, no
change).
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

pytestmark = pytest.mark.asyncio

SCHEMA_NOTE = """---
title: Schema
schema:
  required: [title]
  enums:
    status: [active, archived]
    vault_type: [knowledge]
  tags: [ai, machine-learning]
  folders: {}
---
# Schema
"""


@pytest.fixture
def srv(tmp_path, monkeypatch):
    (tmp_path / "METADATA.md").write_text(SCHEMA_NOTE, encoding="utf-8")
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("MCP_NO_AUTH", "true")

    import importlib

    import server

    importlib.reload(server)
    return server


async def approve(message, response_type, params, context):
    return {"approve": True}


async def refuse_in_form(message, response_type, params, context):
    return {"approve": False}


async def decline(message, response_type, params, context):
    return ElicitResult(action="decline")


async def cancel(message, response_type, params, context):
    return ElicitResult(action="cancel")


async def _call(srv, handler, tool, args) -> str:
    async with Client(srv.mcp, elicitation_handler=handler) as c:
        return (await c.call_tool(tool, args)).content[0].text


GATED = [
    ("vault_create_tag", {"tag": "spin-glass"}),
    ("vault_create_folder", {"folder": "06 Research", "vault_type": "knowledge"}),
    ("vault_update_schema", {"field": "status", "value": "blocked"}),
]


@pytest.mark.parametrize("tool,args", GATED)
async def test_unattended_agent_cannot_change_the_schema(srv, tool, args):
    """The load-bearing case: no human, so no elicitation, so no change."""
    async with Client(srv.mcp) as c:  # no elicitation_handler = no human
        out = (await c.call_tool(tool, args)).content[0].text

    assert "requires human approval" in out
    assert srv._schema().known_tag("spin-glass") is False
    assert "blocked" not in srv._schema().enums["status"]


@pytest.mark.parametrize("tool,args", GATED)
async def test_declining_changes_nothing(srv, tool, args):
    assert "Declined" in await _call(srv, decline, tool, args)
    assert "blocked" not in srv._schema().enums["status"]


@pytest.mark.parametrize("tool,args", GATED)
async def test_cancelling_changes_nothing(srv, tool, args):
    assert "Cancelled" in await _call(srv, cancel, tool, args)


async def test_accepting_the_form_but_unticking_approve_changes_nothing(srv):
    out = await _call(srv, refuse_in_form, "vault_create_tag", {"tag": "spin-glass"})
    assert "Declined" in out
    assert srv._schema().known_tag("spin-glass") is False


async def test_approval_registers_the_tag(srv):
    out = await _call(srv, approve, "vault_create_tag", {"tag": "spin-glass"})
    assert "Registered" in out
    assert srv._schema().known_tag("spin-glass")


async def test_approval_maps_the_folder_to_a_vault_type(srv):
    out = await _call(srv, approve, "vault_create_folder", {"folder": "06 Research", "vault_type": "knowledge"})
    assert "Created" in out
    assert srv._schema().folders["06 Research"] == "knowledge"


async def test_approval_extends_the_enum(srv):
    out = await _call(srv, approve, "vault_update_schema", {"field": "status", "value": "blocked"})
    assert "allowed" in out
    assert "blocked" in srv._schema().enums["status"]


async def test_near_duplicate_tag_is_refused_before_a_human_is_even_asked(srv):
    """Don't waste a human's attention on a tag we can already tell is a duplicate."""
    asked = False

    async def spy(message, response_type, params, context):
        nonlocal asked
        asked = True
        return {"approve": True}

    out = await _call(srv, spy, "vault_create_tag", {"tag": "machinelearning"})
    assert "closely resembles" in out
    assert "machine-learning" in out
    assert not asked, "the human was prompted for a tag we already knew was a duplicate"


async def test_force_lets_a_near_duplicate_through_but_still_needs_approval(srv):
    out = await _call(srv, decline, "vault_create_tag", {"tag": "machinelearning", "force": True})
    assert "Declined" in out  # reached the gate, and the gate held


async def test_folder_with_an_unknown_vault_type_is_refused(srv):
    out = await _call(srv, approve, "vault_create_folder", {"folder": "06 X", "vault_type": "nonsense"})
    assert "not allowed" in out


async def test_update_schema_rejects_a_field_that_is_not_a_vocabulary(srv):
    out = await _call(srv, approve, "vault_update_schema", {"field": "title", "value": "x"})
    assert "not a controlled vocabulary" in out


# --- redirect_uri policy ----------------------------------------------------
#
# Not about approval, but it lives here because this file already owns the
# "reload server with env set" fixture. The sentinel is the kind of thing that
# fails closed and therefore looks fine until the connector it exists for cannot
# log in.


def _reload(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("MCP_NO_AUTH", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import importlib

    import server

    return importlib.reload(server)


@pytest.mark.asyncio
async def test_any_redirect_uri_is_seeded_as_a_list_not_a_bare_sentinel(monkeypatch, tmp_path):
    """origo only sets its wildcard flag when the sentinel is the whole allowlist.

    Handed the bare string it falls back to exact matching against "any" and
    rejects every real redirect_uri -- fail-closed, so not a hole, but it locks
    out exactly the connector MCP_ANY_REDIRECT_URI exists to admit.
    """
    from origo import ANY_REDIRECT_URI

    srv = _reload(monkeypatch, tmp_path, MCP_ANY_REDIRECT_URI="true")
    policy = srv._redirect_policy()

    assert policy == {srv.CLIENT_ID: [ANY_REDIRECT_URI]}
    assert isinstance(policy[srv.CLIENT_ID], list)


@pytest.mark.asyncio
async def test_an_allowlist_is_passed_through(monkeypatch, tmp_path):
    srv = _reload(monkeypatch, tmp_path, MCP_REDIRECT_URIS="https://a.example/cb,https://b.example/cb")
    assert srv._redirect_policy() == {
        srv.CLIENT_ID: ["https://a.example/cb", "https://b.example/cb"]
    }


@pytest.mark.asyncio
async def test_any_redirect_uri_wins_over_an_allowlist(monkeypatch, tmp_path):
    """Asking for "any" while also naming URIs is contradictory; the opt-in wins."""
    from origo import ANY_REDIRECT_URI

    srv = _reload(
        monkeypatch, tmp_path, MCP_ANY_REDIRECT_URI="true", MCP_REDIRECT_URIS="https://a.example/cb"
    )
    assert srv._redirect_policy() == {srv.CLIENT_ID: [ANY_REDIRECT_URI]}


@pytest.mark.asyncio
async def test_no_configuration_means_no_allowlist(monkeypatch, tmp_path):
    """None is origo's accept-anything default, which the startup warning covers."""
    srv = _reload(monkeypatch, tmp_path)
    assert srv._redirect_policy() is None
