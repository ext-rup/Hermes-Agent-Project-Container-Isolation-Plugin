#!/usr/bin/env bash
# Build the Hermes workspace image and verify the tooling actually works.
#
# Apple Container builds via its own buildkit shim; `docker build` through the
# shim is not supported, so this calls `container build` directly.
set -euo pipefail

IMAGE="${HERMES_WORKSPACE_IMAGE:-hermes-workspace:latest}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v container >/dev/null 2>&1; then
    echo "error: Apple 'container' CLI not found on PATH." >&2
    exit 1
fi

echo "==> Building $IMAGE (first build pulls a ~1.4GB base; allow a few minutes)"
container build --tag "$IMAGE" "$DIR"

echo "==> Verifying the tooling"
container run --rm "$IMAGE" /bin/sh -c '
set -e
echo "arch:       $(uname -m)"
echo "python:     $(python3 --version 2>&1)"
echo "node:       $(node --version)"
echo "pdftotext:  $(pdftotext -v 2>&1 | head -1)"
echo "ghostscript:$(gs --version 2>&1)"
python3 - <<PY
import pypdf, pdfplumber, PIL
print("pypdf:     ", pypdf.__version__)
print("pdfplumber:", pdfplumber.__version__)
print("Pillow:    ", PIL.__version__)
PY
'

cat <<EOF

Built $IMAGE.

Point Hermes at it — BOTH places, because ~/.hermes/.env overrides config.yaml:

  # ~/.hermes/.env
  TERMINAL_DOCKER_IMAGE=$IMAGE

  # ~/.hermes/config.yaml
  terminal:
    docker_image: $IMAGE

Then delete the existing containers so each project gets one on the new image
(reuse matches on labels, not image, so they would otherwise keep the old one):

  container list --all
  container stop <id> && container delete <id>

EOF
