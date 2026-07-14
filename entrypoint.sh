#!/bin/sh
set -e

VAULT="${VAULT_PATH:-/vault}"

if [ ! -d "$VAULT" ]; then
  echo "FATAL: vault not mounted at $VAULT" >&2
  exit 1
fi

# The vault is a Syncthing replica owned by the host user. Run as that owner so
# writes land with the uid Syncthing expects, rather than chowning the vault.
VAULT_UID="$(stat -c %u "$VAULT")"
VAULT_GID="$(stat -c %g "$VAULT")"

if [ "$VAULT_UID" = "0" ]; then
  exec python /app/server.py
fi

if ! getent group "$VAULT_GID" >/dev/null 2>&1; then
  groupadd -g "$VAULT_GID" vaultgrp 2>/dev/null || addgroup -g "$VAULT_GID" vaultgrp 2>/dev/null || true
fi
usermod -u "$VAULT_UID" -g "$VAULT_GID" mcpuser 2>/dev/null || true

exec gosu "$VAULT_UID:$VAULT_GID" python /app/server.py
