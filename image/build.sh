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
echo "tesseract:  $(tesseract --version 2>&1 | head -1)"
echo "languages:  $(tesseract --list-langs 2>&1 | tail -n +2 | tr "\n" " ")"
echo "ocrmypdf:   $(ocrmypdf --version 2>&1)"
python3 - <<PY
import pypdf, pdfplumber, PIL
print("pypdf:     ", pypdf.__version__)
print("pdfplumber:", pdfplumber.__version__)
print("Pillow:    ", PIL.__version__)
PY

# End-to-end OCR check: build an image-only PDF (no text layer), confirm
# pdftotext finds nothing, then confirm OCR recovers the text. Version strings
# alone would not catch a missing language pack or a broken ghostscript path.
cd /tmp
python3 - <<PY
from PIL import Image, ImageDraw, ImageFont
font = ImageFont.truetype(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
img = Image.new("RGB", (1400, 300), "white")
ImageDraw.Draw(img).text((40, 100), "SCANNED OCR TEST 4711", fill="black", font=font)
img.save("/tmp/scan.pdf", "PDF", resolution=200)
PY
before=$(pdftotext /tmp/scan.pdf - 2>/dev/null | tr -d "[:space:]")
if [ -n "$before" ]; then
    echo "WARNING: fixture already had a text layer; OCR check is not meaningful" >&2
fi
ocrmypdf --quiet --language eng /tmp/scan.pdf /tmp/scan-ocr.pdf
after=$(pdftotext /tmp/scan-ocr.pdf - 2>/dev/null)
echo "ocr input text:  ${before:-(none, as expected)}"
echo "ocr output text: $(echo "$after" | tr -s "[:space:]" " " | head -c 60)"
case "$after" in
    *4711*) echo "OCR round-trip: OK" ;;
    *)      echo "OCR round-trip: FAILED — text not recovered" >&2; exit 1 ;;
esac
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
