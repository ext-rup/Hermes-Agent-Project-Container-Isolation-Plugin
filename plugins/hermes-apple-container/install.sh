#!/usr/bin/env bash
# Install the Apple Container terminal backend plugin.
#
#   ./install.sh        # from the repo
#   bash ~/.hermes/plugins/hermes-apple-container/install.sh  # from the installed copy
#
# Copies the plugin into ~/.hermes/plugins/, adds it to plugins.enabled,
# sets terminal.backend, clears any stale HERMES_DOCKER_BINARY, and restarts
# the gateway.  Idempotent — safe to re-run.
set -euo pipefail

PLUGIN="hermes-apple-container"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="${HERMES_PLUGIN_DIR:-$HOME/.hermes/plugins}"
CONFIG="${HERMES_CONFIG:-$HOME/.hermes/config.yaml}"
ENV_FILE="${HERMES_HOME:-$HOME/.hermes}/.env"

# --- Tests ------------------------------------------------------------------
if [[ -f "$SRC/../../tests/test_project_scope.py" ]]; then
    echo "==> Running tests"
    python3 -m unittest discover -s "$SRC/../../tests" -q
fi

# --- Sync shared core -------------------------------------------------------
if [[ -f "$SRC/../../plugins/project_scope.py" ]]; then
    echo "==> Syncing project_scope.py"
    cp "$SRC/../../plugins/project_scope.py" "$SRC/project_scope.py"
fi

# --- Copy -------------------------------------------------------------------
echo "==> Installing $PLUGIN -> $PLUGIN_ROOT/$PLUGIN"
mkdir -p "$PLUGIN_ROOT"
rm -rf "${PLUGIN_ROOT:?}/$PLUGIN"
cp -R "$SRC" "$PLUGIN_ROOT/$PLUGIN"
chmod +x "$PLUGIN_ROOT/$PLUGIN/docker-wrapper" 2>/dev/null || true

# --- Remove conflicting plugins --------------------------------------------
for OTHER in hermes-projects-apple hermes-projects-docker; do
    if [[ -e "$PLUGIN_ROOT/$OTHER" ]]; then
        echo "==> Removing conflicting plugin ($OTHER)"
        rm -rf "$PLUGIN_ROOT/$OTHER"
    fi
done

# --- Revert the old file patch if present ----------------------------------
if [[ -f "$SRC/../../patches/per-project-task-id.py" ]]; then
    python3 "$SRC/../../patches/per-project-task-id.py" --revert 2>/dev/null || true
fi

# --- Enable in config.yaml -------------------------------------------------
if grep -qE "^plugins:" "$CONFIG" 2>/dev/null; then
    if grep -q "$PLUGIN" "$CONFIG"; then
        echo "==> $PLUGIN already in plugins.enabled"
    else
        echo
        echo "!! A 'plugins:' block already exists in $CONFIG."
        echo "   Add this entry by hand, then restart the gateway:"
        echo
        echo "   plugins:"
        echo "     enabled:"
        echo "       - $PLUGIN"
        echo
        exit 1
    fi
else
    echo "==> Enabling $PLUGIN in $CONFIG"
    cp "$CONFIG" "$CONFIG.pre-plugins.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    printf '\nplugins:\n  enabled:\n    - %s\n' "$PLUGIN" >> "$CONFIG"
fi

# --- Set terminal.backend --------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
    current="$(hermes config get terminal.backend 2>/dev/null || echo "")"
    if [[ "$current" == "apple_container" ]]; then
        echo "==> terminal.backend already set to apple_container"
    elif [[ -z "$current" || "$current" == "local" ]]; then
        hermes config set terminal.backend apple_container
        echo "==> terminal.backend -> apple_container"
    else
        echo "!! terminal.backend is '$current' — not overriding."
        echo "   To use Apple Container: hermes config set terminal.backend apple_container"
    fi
else
    echo "==> (hermes CLI not on PATH — set terminal.backend manually if needed)"
fi

# --- Clear stale HERMES_DOCKER_BINARY --------------------------------------
if [[ -f "$ENV_FILE" ]] && grep -q "HERMES_DOCKER_BINARY" "$ENV_FILE"; then
    echo "==> Clearing stale HERMES_DOCKER_BINARY from $ENV_FILE"
    sed -i.bak '/HERMES_DOCKER_BINARY/d' "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
fi

# --- Restart the gateway ----------------------------------------------------
echo "==> Restarting the gateway"
launchctl kickstart -k "gui/$(id -u)/ai.hermes.gateway" || true
sleep 5

cat <<EOF

Installed $PLUGIN.

  * terminal.backend: apple_container (first-class provider)
  * No Hermes source files modified — 'hermes update' won't clobber this.
  * Per-project isolation: each project chat gets its own container.

Verify:

  container list --all
  grep -a "apple_container\|per-project scoping" ~/.hermes/logs/agent.log | tail

EOF
