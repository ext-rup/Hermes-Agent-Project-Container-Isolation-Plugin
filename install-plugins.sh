#!/usr/bin/env bash
# Install per-project container scoping as a Hermes plugin.
#
#   ./install-plugins.sh apple    # Apple Container — first-class provider backend (v2)
#   ./install-plugins.sh apple-classic  # Apple Container — old shim-only plugin (v1)
#   ./install-plugins.sh docker   # real Docker — no shim needed
#
# The plugin replaces patches/per-project-task-id.py: it does the same job
# without editing Hermes' tracked files, so `hermes update` no longer clobbers
# it, and it resolves the project per session rather than from one global
# active_id (so concurrent projects don't share a container).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="${1:-apple}"
PLUGIN_ROOT="${HERMES_PLUGIN_DIR:-$HOME/.hermes/plugins}"
CONFIG="${HERMES_CONFIG:-$HOME/.hermes/config.yaml}"

case "$BACKEND" in
    apple)
        PLUGIN="hermes-apple-container"
        OTHERS=("hermes-projects-apple" "hermes-projects-docker")
        ;;
    apple-classic)
        PLUGIN="hermes-projects-apple"
        OTHERS=("hermes-projects-docker" "hermes-apple-container")
        ;;
    docker)
        PLUGIN="hermes-projects-docker"
        OTHERS=("hermes-projects-apple" "hermes-apple-container")
        ;;
    *) echo "usage: $0 [apple|apple-classic|docker]" >&2; exit 1 ;;
esac

SRC="$REPO_DIR/plugins/$PLUGIN"
[[ -d "$SRC" ]] || { echo "error: $SRC not found" >&2; exit 1; }

echo "==> Running tests"
python3 -m unittest discover -s "$REPO_DIR/tests" -q

# project_scope.py is maintained once and copied in, so the plugins can
# never drift apart.
echo "==> Syncing shared core into $PLUGIN"
cp "$REPO_DIR/plugins/project_scope.py" "$SRC/project_scope.py"
# Plugins that bundle the shim need it executable.
if [[ -f "$SRC/docker-wrapper" ]]; then
    chmod +x "$SRC/docker-wrapper" 2>/dev/null || true
fi

echo "==> Reverting the file patch (the plugin supersedes it)"
python3 "$REPO_DIR/patches/per-project-task-id.py" --revert

mkdir -p "$PLUGIN_ROOT"
for OTHER in "${OTHERS[@]}"; do
    if [[ -e "$PLUGIN_ROOT/$OTHER" ]]; then
        echo "==> Removing conflicting plugin ($OTHER)"
        rm -rf "$PLUGIN_ROOT/$OTHER"
    fi
done

echo "==> Installing $PLUGIN -> $PLUGIN_ROOT/$PLUGIN"
rm -rf "${PLUGIN_ROOT:?}/$PLUGIN"
cp -R "$SRC" "$PLUGIN_ROOT/$PLUGIN"

# plugins.enabled is an opt-in allow-list. Append a block only when absent —
# rewriting the YAML would strip the file's comments.
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
    cp "$CONFIG" "$CONFIG.pre-plugins.$(date +%Y%m%d%H%M%S)"
    printf '\nplugins:\n  enabled:\n    - %s\n' "$PLUGIN" >> "$CONFIG"
fi

echo "==> Restarting the gateway"
launchctl kickstart -k "gui/$(id -u)/ai.hermes.gateway" || true
sleep 5

cat <<EOF

Installed $PLUGIN.

  * No Hermes source files are modified — 'hermes update' won't clobber this,
    and the post-update patch step is no longer needed.
  * The project is resolved per session (state.db sessions.cwd), so projects
    running concurrently get their own containers.

Verify — send a message from a project chat, then:

  grep -a "Creating new .* environment for task" ~/.hermes/logs/agent.log | tail -3
  container list --all

EOF
if [[ "$BACKEND" == "apple" ]]; then
cat <<'EOF'
Select the new backend:

  hermes config set terminal.backend apple_container

The provider registers itself automatically; no HERMES_DOCKER_BINARY env var
is needed — the shim is bundled inside the plugin.

If you previously used the old hermes-projects-apple plugin, also clear any
HERMES_DOCKER_BINARY you may have set in ~/.hermes/.env — the provider handles
shim resolution internally, and an explicit value would still outrank it.

EOF
fi
if [[ "$BACKEND" == "apple-classic" ]]; then
cat <<'EOF'
The classic Apple plugin hijacks terminal.backend: docker via HERMES_DOCKER_BINARY.
The new hermes-apple-container plugin (./install-plugins.sh apple) registers a
first-class apple_container backend instead — prefer it on Hermes 0.21+.
EOF
fi
if [[ "$BACKEND" == "docker" ]]; then
cat <<'EOF'
Docker backend needs the workspace mount to follow the project:

  hermes config set terminal.docker_mount_cwd_to_workspace true

and no fixed ":/workspace" entry in terminal.docker_volumes. The plugin logs a
warning at startup if either is wrong.
EOF
fi
