"""Change detection by polling: a JSON journal, diffed against the current tree.

No watcher, no event bus, no database. `scan()` asks the document service for the
current set of managed documents and compares it with what it saw last time. What
differs gets the next sequence number. A consumer polls with an opaque cursor and
receives everything numbered above it.

That is deliberately the least machinery that satisfies the requirement. The vault
is a Syncthing replica edited by a human in Obsidian: changes arrive in bursts
separated by minutes, inotify would fire midway through Syncthing's own temp-file
dance, and the `Vault` index is already mtime-driven, so a scan on an unchanged
vault is a directory walk and a stat per note. Eventual consistency is not a
compromise here, it is the shape of the underlying system.

Recovery is the other reason for this design. The only durable state is one JSON
file whose loss is survivable by construction: a new `epoch` invalidates outstanding
cursors, consumers are told `reset`, and they re-ingest. There is no repair
procedure to get wrong at 3am.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path

from documents import DocumentRef, DocumentService

logger = logging.getLogger("miradian.changes")

STATE_FILENAME = "changes.json"
STATE_VERSION = 1
DEFAULT_TOMBSTONE_LIMIT = 5000

CREATED, UPDATED, MOVED, DELETED = "created", "updated", "moved", "deleted"


class ChangeJournal:
    """Sequence-numbered change log over a DocumentService's managed documents.

    One entry per document, holding its *most recent* change. A consumer that is
    behind by ten updates to the same document gets one entry, not ten -- which is
    what it wants, because it is going to re-read the document anyway. This is a
    change *feed*, not an audit log; the vault's git history is the audit log.
    """

    def __init__(
        self,
        service: DocumentService,
        state_dir: str | Path,
        *,
        tombstone_limit: int = DEFAULT_TOMBSTONE_LIMIT,
    ):
        self.service = service
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.tombstone_limit = tombstone_limit
        self._lock = threading.RLock()

        # The journal must not live inside the vault. Syncthing would replicate it
        # to every machine, each writing its own sequence numbers into the same
        # file, and git would track a file that changes on every poll.
        vault_root = self.service.vault.root
        if self.state_dir == vault_root or vault_root in self.state_dir.parents:
            raise ValueError(
                f"STATE_DIR ({self.state_dir}) is inside the vault ({vault_root}). "
                "The change journal must live outside the vault, or Syncthing will "
                "replicate it and git will track it."
            )

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / STATE_FILENAME
        self._state = self._load()

    # --- state -------------------------------------------------------------

    def _fresh(self) -> dict:
        return {"version": STATE_VERSION, "epoch": uuid.uuid4().hex, "seq": 0, "entries": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._fresh()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # A corrupt journal is recoverable by throwing it away: consumers get a
            # new epoch, hear `reset`, and re-ingest. Refusing to start instead
            # would turn a disposable cache into an outage.
            logger.warning("change journal unreadable (%s); starting a new epoch", e)
            return self._fresh()
        if state.get("version") != STATE_VERSION or "entries" not in state:
            logger.warning("change journal has an unknown format; starting a new epoch")
            return self._fresh()
        return state

    def _save(self) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            # Losing the journal costs a resync, not correctness. Do not fail the
            # caller's poll over it.
            logger.warning("could not persist the change journal: %s", e)
        finally:
            Path(tmp).unlink(missing_ok=True)

    @property
    def epoch(self) -> str:
        return self._state["epoch"]

    # --- scanning ----------------------------------------------------------

    @staticmethod
    def _key(ref: DocumentRef) -> str:
        """The journal key: the uid when there is one.

        With uid assignment switched off there is no immutable identity to key on,
        so the path stands in and a move reads as delete+create. That is the honest
        consequence of turning identity off, not a silent downgrade.
        """
        return ref.id or f"path:{ref.path}"

    def _entry(self, ref: DocumentRef, change: str, seq: int, previous_path: str | None = None) -> dict:
        return {
            "type": change,
            "seq": seq,
            "id": ref.id,
            "path": ref.path,
            "previous_path": previous_path,
            "project": ref.project,
            "doc_type": ref.doc_type,
            "title": ref.title,
            "version": ref.version,
            "mtime": ref.mtime,
        }

    def scan(self) -> int:
        """Diff the tree against the journal. Returns how many changes were recorded.

        Called at the start of every poll rather than on a timer: with no consumer
        asking, there is nothing a scan could usefully do, and a lazy scan cannot
        drift out of step with the answers it produces.
        """
        with self._lock:
            current = {self._key(r): r for r in self.service.managed_documents()}
            entries: dict = self._state["entries"]
            seq = self._state["seq"]
            recorded = 0

            for key, ref in current.items():
                prev = entries.get(key)
                if prev is None or prev["type"] == DELETED:
                    change, previous_path = CREATED, None
                elif prev["path"] != ref.path:
                    # Identity is the uid, so this is exact: the same document is
                    # somewhere else now. No content-hash guessing, and a document
                    # that moved *and* changed is still one event.
                    change, previous_path = MOVED, prev["path"]
                elif prev["version"] != ref.version:
                    change, previous_path = UPDATED, None
                else:
                    continue
                seq += 1
                entries[key] = self._entry(ref, change, seq, previous_path)
                recorded += 1

            for key in [k for k, e in entries.items() if k not in current and e["type"] != DELETED]:
                seq += 1
                entries[key] = {**entries[key], "type": DELETED, "seq": seq, "previous_path": None}
                recorded += 1

            if recorded:
                self._state["seq"] = seq
                self._prune(entries)
                self._save()
            return recorded

    def _prune(self, entries: dict) -> None:
        """Cap the tombstones. Live entries are never dropped.

        A dropped tombstone means a consumer that has been away long enough may not
        hear about that deletion -- but a consumer away that long is already past
        the point where a full resync is the right answer.
        """
        dead = sorted(
            (k for k, e in entries.items() if e["type"] == DELETED),
            key=lambda k: entries[k]["seq"],
        )
        for key in dead[: max(0, len(dead) - self.tombstone_limit)]:
            del entries[key]

    # --- polling -----------------------------------------------------------

    def cursor(self) -> str:
        with self._lock:
            return f"{self._state['epoch']}:{self._state['seq']}"

    def _parse(self, cursor: str | None) -> int | None:
        """The sequence a cursor refers to, or None if it cannot be honoured."""
        if not cursor:
            return None
        epoch, _, raw = cursor.partition(":")
        if epoch != self._state["epoch"]:
            return None  # different epoch: the journal was rebuilt under them
        try:
            seq = int(raw)
        except ValueError:
            return None
        if seq > self._state["seq"]:
            return None  # from the future: a rolled-back journal, so resync
        return seq

    def poll(self, cursor: str | None = None, limit: int = 500) -> dict:
        """Everything that changed after `cursor`.

        A missing, stale or unparseable cursor yields `reset: true` and the full
        current document set, which is the only recovery a consumer has to
        implement: drop what you have and take what comes back.
        """
        with self._lock:
            self.scan()
            entries: dict = self._state["entries"]
            since = self._parse(cursor)
            reset = since is None

            if reset:
                # Tombstones are omitted: a consumer starting from nothing has no
                # record of the deleted document and telling it about the deletion
                # would only be noise.
                pool = [e for e in entries.values() if e["type"] != DELETED]
                pool = [{**e, "type": CREATED, "previous_path": None} for e in pool]
            else:
                pool = [e for e in entries.values() if e["seq"] > since]

            pool.sort(key=lambda e: e["seq"])
            window = pool[:limit]
            has_more = len(pool) > len(window)

            if window:
                next_seq = window[-1]["seq"]
            else:
                next_seq = self._state["seq"] if reset else since

            return {
                "changes": window,
                "cursor": f"{self._state['epoch']}:{next_seq}",
                "reset": reset,
                "has_more": has_more,
            }
