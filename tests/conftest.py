"""Shared test setup.

Importing `server` builds a ChangeJournal, which creates its state directory. Left
to the default that is the developer's real `~/.local/state/miradian`, so every test
run would litter a live machine and, worse, share one journal between unrelated
tests. Autouse, so a test that reloads `server` cannot forget it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path_factory.mktemp("state")))
