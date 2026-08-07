#!/usr/bin/env bash
# Install the Apple Container shim for Hermes Agent.
#
# Hermes resolves its container runtime through find_docker()
# (tools/environments/docker.py), whose first step is the HERMES_DOCKER_BINARY
# env var — read from ~/.hermes/.env at runtime, ahead of PATH.
#
# That variable is normally already set to ~/.hermes/docker-wrapper, so this
# installer just points that path at this repo. No sudo, no PATH surgery, no
# service restart: the shim is a fresh process on every docker call, so the new
# code is live for the next container Hermes creates.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$REPO_DIR/docker-wrapper"
ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
TARGET="${HERMES_SHIM_TARGET:-$HOME/.hermes/docker-wrapper}"

chmod +x "$WRAPPER"

if ! command -v container >/dev/null 2>&1; then
    echo "error: Apple 'container' CLI not found on PATH." >&2
    exit 1
fi

echo "==> Running tests"
python3 -m unittest discover -s "$REPO_DIR/tests" -q

# Confirm HERMES_DOCKER_BINARY actually points where we're about to install.
# An uncommented assignment to some other path would silently win.
configured=""
if [[ -f "$ENV_FILE" ]]; then
    configured="$(grep -E '^[[:space:]]*HERMES_DOCKER_BINARY=' "$ENV_FILE" \
        | tail -1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
fi
configured="${configured/#\~/$HOME}"

if [[ -z "$configured" ]]; then
    echo "==> HERMES_DOCKER_BINARY is not set in $ENV_FILE; adding it"
    printf '\nHERMES_DOCKER_BINARY=%s\n' "$TARGET" >> "$ENV_FILE"
elif [[ "$configured" != "$TARGET" ]]; then
    echo "warning: $ENV_FILE points HERMES_DOCKER_BINARY at:" >&2
    echo "           $configured" >&2
    echo "         but this installs to:" >&2
    echo "           $TARGET" >&2
    echo "         Repoint the .env line, or re-run with HERMES_SHIM_TARGET set." >&2
    exit 1
fi

if [[ -e "$TARGET" && ! -L "$TARGET" ]]; then
    BACKUP="$TARGET.pre-rewrite.$(date +%Y%m%d%H%M%S)"
    echo "==> Backing up $TARGET -> $BACKUP"
    cp "$TARGET" "$BACKUP"
fi

# Symlink so repo edits are live without reinstalling.
echo "==> Linking $TARGET -> $WRAPPER"
ln -sfn "$WRAPPER" "$TARGET"

echo "==> Applying the per-project task-id patch to Hermes"
# Idempotent; re-run after every `hermes update`, which git-pulls over it.
python3 "$REPO_DIR/patches/per-project-task-id.py"

echo "==> Verifying"
"$TARGET" version

cat <<EOF

Done. Nothing else on the system was touched:

  * /usr/local/bin/docker            (Docker Desktop) — untouched
  * anything on PATH                 — untouched; HERMES_DOCKER_BINARY outranks it
  * the gateway service              — not restarted; not needed

Remaining steps:

  1. Remove the hardcoded workspace mounts from ~/.hermes/config.yaml:
       terminal.docker_volumes:   <- delete the ":/workspace" entries

  2. Retire the containers created before per-project scoping. They carry an
     unscoped hermes-task-id label and will never be reused again:
       container list --all
       container stop <id> && container delete <id>

  3. Send a message from a project chat, then confirm the routing:
       "$TARGET" ps -a --filter label=hermes-agent=1 \\
          --format '{{.ID}}	{{.Label "hermes-task-id"}}'

To roll back: restore the .pre-rewrite.* backup over $TARGET.
EOF
