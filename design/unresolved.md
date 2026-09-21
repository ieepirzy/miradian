# Unresolved decisions

Open questions from the structured-document work. Each says what was shipped in the
meantime, so nothing is blocked on an answer.

## 1. Should uid stamping be opt-in per vault rather than on by default?

Miradian writes a `uid:` into any document that declares `doc_type`. That is a
side effect of *reading*, and it modifies files a human owns and edits.

Shipped: on by default, `ASSIGN_UID=false` to disable, and only ever applied to
documents that already carry the marker field. Ordinary notes are never touched.

The question is whether the first poll against a vault with hundreds of existing
managed documents should stamp them all at once — currently it does, in one burst,
which Syncthing will propagate as hundreds of small changes. There is no such vault
today (`09 Tasks` is empty), so this is untested at scale.

## 2. Should `links` be exposed on the generic API?

Miradian has a full wikilink graph (backlinks, unresolved links, neighbourhoods).
MiraDesign says it will consume one if offered and degrade if not.

Shipped: `links: false` in the capabilities block; nothing exposed.

The hesitation is that the graph is Obsidian's, not Markdown's: `[[Target]]`
resolution by stem with shallowest-wins is an Obsidian rule. Exposing it generically
means either leaking that rule or inventing a "document reference" abstraction that
happens to be implemented by wikilinks. The second is defensible but is a design
decision, not a wiring job.

## 3. Should `search` be exposed?

`Vault.search` already does content + frontmatter grep with filters. It would be a
thin endpoint.

Shipped: `search: false`. Left out to keep this change to the stated scope; it is
genuinely small if wanted.

## 4. What happens when a project directory is renamed?

Documents keep their uids — identity is per document, not per project. But the
project `id` *is* its directory path, so renaming a project makes every consumer's
stored project reference dangle, and the change feed reports every document in it as
`moved` with no signal that they moved together.

Shipped: nothing. Projects have no uid.

The options are a project manifest document (`_project.md` with its own uid), or
accepting that project ids are paths and consumers re-resolve on rename. The first
adds required ceremony to "a project is a directory", which was an explicit goal.

## 5. Where does coordination state live? — answered for now

Status, priority, assignment, presence, review state, cursors and the event log stay
in MiraDesign's Postgres; Markdown holds narrative only, referenced by `doc_ref`.
Confirmed by MiraDesign 2026-09-17; it is their decision (their D1) and they have
committed to saying so before any change ships rather than after.

That answer is what the polling design is sized for: human-paced document creation
and metadata patches. If it ever inverts and task status moves into frontmatter,
the write volume changes character completely — machine-paced field updates, each
one replicated by Syncthing — and poll-based change detection would need revisiting
along with the atomic-write-per-field cost.

Miradian assumes neither way. The entry stays open because the *consequence* is
Miradian's problem even though the decision is not.

Related, and explicitly **not** Miradian's to solve: what a consumer does when a
human deletes a `uid:` line by hand and a stored reference orphans. MiraDesign
degrades deliberately — the document 404s, metadata comes back null, the board
still renders. A sidecar index that could itself drift would be worse than an
honest 404, so nothing is defended against here.

## 6. Does the `.trash/` delete semantic belong on the generic API?

`delete_document` moves the file to the vault's `.trash/`, and the response carries
`recoverable_at`. That is a vault convention leaking through a generic verb.

Shipped: the field is documented as informational. A consumer that ignores it sees
an ordinary delete.

## 7. Multiple project roots

`PROJECT_ROOTS` accepts a `::`-separated list and the code handles several, but only
one is configured and only one is tested end to end. Two roots containing
same-named project directories would produce two distinct projects with distinct ids
(the ids include the root), which is correct but unexercised.

## 8. No client library

MiraDesign writes its own HTTP adapter. That is the right boundary, but it means the
API contract is enforced only by `docs/api.md` and good intentions. If a second
consumer appears, a small typed client or an OpenAPI document earns its keep.

## 9. The fastmcp 4.x / mcp 2.x port — done

Shipped 2026-09-21. `requirements.txt` now pins `fastmcp>=4,<5` and `mcp>=2,<3`.

The trigger: a client negotiating protocol era 2026-07-28 (`mcp` 2.x's
`LATEST_PROTOCOL_VERSION`) was rejected outright at the transport layer by the old
`mcp>=1.28,<2` pin, which tops out at `2025-11-25` — every tool, not just the
approval-gated ones, was unreachable for any client on current-generation SDKs.

The two disagreements this pin previously avoided turned out to be one real and one
already handled:

- `mcp` 2.x renamed `McpError` to `MCPError` — `server.py` already imported either
  defensively, so this was never actually a blocker.
- Under fastmcp 4.x every `ctx.elicit` call raises for a client on the 2026-07-28
  era specifically — not a fastmcp bug, see #12: the spec removed the back-channel
  it needs. 186/197 tests pass unchanged; the 11 that exercise an elicitation
  accept/decline/cancel round-trip are `xfail(strict=True)` in
  `tests/test_approval.py`, not deleted — see #12.

## 10. Stateful MCP sessions — needs Ilari's call

Under stateless streamable HTTP the server cannot receive a client's answer to a
request *it* made: every elicitation response arrives with a request id no session
remembers, the session crashes, and the tool call hangs forever. All three
approval-gated tools have therefore never worked over the real transport — they
only ever worked in the in-process tests.

`MCP_STATELESS_HTTP=false` fixes it, verified end to end in the container (refuses
unattended, registers with a human).

Shipped: the default stays `true`, i.e. what is deployed today. Turning it off is a
genuine change to the transport — the client must carry `Mcp-Session-Id`, sessions
live in the memory of the process that handled `initialize`, a restart invalidates
outstanding ones, and more than one replica needs session affinity. The claude.ai
remote connector is the client that matters and cannot be tested from a dev box.

So: flip it, confirm the connector still works, and keep it off. The gate fails
closed either way, so leaving it on regresses nothing that ever worked — it just
leaves `vault_create_tag`, `vault_create_folder` and `vault_update_schema`
unusable, which is the status quo.

Superseded in part by #12: for any client on the 2026-07-28 protocol era, stateful
sessions would no longer be enough on their own — imperative `ctx.elicit` fails
closed on that era regardless of transport, because the spec removed the
back-channel it needs, not because the session forgot the request id. Flipping
`MCP_STATELESS_HTTP` would only restore the gate for clients still negotiating an
older era, which may be none in practice by the time anyone gets to this.

## 12. Elicitation is gone for modern clients — the approval gate needs the guard pattern

The three approval-gated tools' human-in-the-loop gate (`_require_approval` in
`server.py`) uses imperative `ctx.elicit`: ask a mid-tool-call question, block on
the answer. Protocol era 2026-07-28 (SEP-2322/2575) removed the server-initiated
back-channel that requires, permanently — not a version fastmcp or mcp can restore
by patching. fastmcp 4.x detects a modern-era connection and raises a clear error
instead of hanging (`elicitation via server-initiated requests is unavailable on
2026-07-28 connections`), which `_require_approval`'s existing `except (McpError,
ToolError)` already folds into the same "no elicitation support" refusal it always
gave unattended agents. So today: fails closed, safely, for every client on this
SDK generation — human-attended or not, stateless or stateful. Nothing unsafe, but
`vault_create_tag` and friends are unusable for real deployed clients, which by now
all negotiate the modern era.

fastmcp's replacement is the guard/background-task pattern: a tool returns
`InputRequiredResult` instead of blocking, and reads the human's answer from
`ctx.input_responses` / `ctx.request_state` on a later re-run. Restoring the
approval gate for modern clients means rewriting `_require_approval` around that,
which changes the tool's call shape (a client must now poll or resume a background
task rather than getting one synchronous response) — a real design change, not a
drop-in swap. Unscheduled; the `xfail(strict=True)` tests in `tests/test_approval.py`
will start failing (i.e. unexpectedly passing) the day this lands, which is the
point.

## 11. Three writers, and the one that cannot participate

The deployed vault has three writers on the same files: a human in Obsidian, agents
over MCP, and MiraDesign over HTTP.

What is actually true today:

- Every write is atomic (temp file + `os.replace`), so no reader ever observes a
  half-written document and Syncthing never sees a torn file.
- `expected_version` turns a detected race into a `409`.
- `/api/v1` **refuses** an update that passes neither `expected_version` nor
  `"overwrite": true`, so a consumer cannot reach last-writer-wins by forgetting a
  parameter — only by asking for it.
- Every read-modify-write on both surfaces now happens under one lock acquisition
  (`Vault.edit`), so two callers cannot mutate the same live ruamel `CommentedMap`
  at once. There are concurrency tests now, though they are honest about what they
  show: they pin the behaviour, not the lock placement, because the vault index
  lock already serialises a read against an in-flight write.

What that does **not** give you: atomicity means nobody sees a half-written file, not
that two writers cannot each land a complete write where the second silently discards
the first's change.

Worse, the one writer that *cannot* participate is the human: Obsidian does not know
what `expected_version` is. A human editing a document while MiraDesign patches its
metadata is a real lost update, and the only reason it has not happened is that
nothing is deployed yet.

Shipped: nothing. Out of scope deliberately rather than by oversight — a real answer
is either file locking (which Syncthing complicates), a merge strategy, or accepting
it and detecting after the fact from the change feed. Recorded so the first
occurrence is a known unknown rather than a surprise.

## 12. Rate of scanning

Every poll triggers a full directory walk plus a stat per note (~30 ms at 1k notes,
~270 ms at 10k). At one consumer polling every few seconds this is free. It is not
free at ten consumers polling every second, and nothing currently rate-limits it or
caches the scan between near-simultaneous polls.
