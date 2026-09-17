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

## 5. Where does coordination state live?

MiraDesign is keeping status, priority, assignment and review state in Postgres and
using Markdown for narrative only. If that inverts — if task status belongs in
frontmatter — the write volume against Miradian changes character completely: from
human-paced document edits to machine-paced field updates, with Syncthing
replicating every one of them.

Shipped: nothing assumes either way. This is MiraDesign's decision (their D1), but
the answer determines whether poll-based change detection stays adequate.

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

## 9. The fastmcp 4.x / mcp 2.x port

`requirements.txt` now pins `fastmcp>=3.4,<4` and `mcp>=1.28,<2`. Unpinned, the
container resolved 4.x/2.x while the tests ran against 3.x/1.x, and the two
disagree twice over:

- `mcp` 2.x renamed `McpError` to `MCPError` (server.py now imports either).
- Under fastmcp 4.x every `ctx.elicit` call raises, so all three human-approval
  tools refuse unconditionally. It fails closed, so nothing becomes unsafe, but
  `vault_create_tag` and friends stop working.

Moving to 4.x is a deliberate port with its own testing, not something to let a
resolver decide during a `docker build`. Unscheduled.

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

## 11. Rate of scanning

Every poll triggers a full directory walk plus a stat per note (~30 ms at 1k notes,
~270 ms at 10k). At one consumer polling every few seconds this is free. It is not
free at ten consumers polling every second, and nothing currently rate-limits it or
caches the scan between near-simultaneous polls.
