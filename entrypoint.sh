#!/bin/sh
set -e

VAULT="${VAULT_PATH:-/vault}"
# origo's own env var for where it persists OAuth state; unset here only
# picks the /data default set in compose.yml -- ORIGO_STORAGE_PATH="" (forcing
# in-memory storage) is left alone, since there's then no directory to fix.
DATA="${ORIGO_STORAGE_PATH-/data}"

if [ ! -d "$VAULT" ]; then
  echo "FATAL: vault not mounted at $VAULT" >&2
  exit 1
fi

# Run as whoever owns the vault on the host, so notes we write keep the same
# ownership as notes you write. Adopting the directory's uid beats chowning it:
# the vault is yours, and a sync client (Syncthing, Dropbox) or Obsidian itself
# may be writing to it at the same time and expects its own uid back.
VAULT_UID="$(stat -c %u "$VAULT")"
VAULT_GID="$(stat -c %g "$VAULT")"

# Unlike the vault, origo's persisted OAuth state is ours alone -- nothing else
# writes to it -- so chowning it to match is fine (and simpler). Without this,
# a fresh Docker volume stays root-owned, the dropped-privilege process below
# can't write to it, and origo silently falls back to in-memory storage.
if [ -n "$DATA" ]; then
  mkdir -p "$DATA"
  chown "$VAULT_UID:$VAULT_GID" "$DATA" 2>/dev/null || true
fi

if [ "$VAULT_UID" = "0" ]; then
  exec python /app/server.py
fi

if ! getent group "$VAULT_GID" >/dev/null 2>&1; then
  groupadd -g "$VAULT_GID" vaultgrp 2>/dev/null || addgroup -g "$VAULT_GID" vaultgrp 2>/dev/null || true
fi
usermod -u "$VAULT_UID" -g "$VAULT_GID" mcpuser 2>/dev/null || true

exec gosu "$VAULT_UID:$VAULT_GID" python /app/server.py
