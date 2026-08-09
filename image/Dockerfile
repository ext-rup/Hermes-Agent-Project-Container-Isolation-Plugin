# Workspace image for Hermes Agent containers.
#
# Hermes' default image (nikolaik/python-nodejs) carries Python and Node but no
# document tooling, so anything beyond reading a PDF — extracting text, merging,
# rasterising — fails in the sandbox unless the agent installs packages first.
# Those installs are lost whenever a container is recreated, which happens on
# every image change and whenever a project's container is cleaned up.
#
# Baking them in makes PDF work available immediately and offline.
#
# Build:  ./image/build.sh
# Use:    terminal.docker_image: hermes-workspace:latest
FROM nikolaik/python-nodejs:python3.13-nodejs24

# poppler-utils  → pdftotext, pdftoppm, pdfimages, pdfinfo
# ghostscript    → PDF/PS conversion and compression, used by several Python libs
# libgl1/libglib → runtime deps Pillow and pdfplumber pull in for image work
#
# OCR, for scanned PDFs that carry no text layer (pdftotext returns nothing):
# tesseract-ocr      → the OCR engine
# tesseract-ocr-eng  → English language data
# tesseract-ocr-deu  → German language data
#   Language data must be present at build time; add more packs here (the
#   Debian names follow tesseract-ocr-<iso639-2>, e.g. -fra, -spa, -nld).
# qpdf / unpaper / pngquant → ocrmypdf's helpers for linearising, deskewing
#   and lossily optimising the output
# fonts-dejavu-core  → a real TTF, so rasterised pages and generated test
#   images render legibly rather than as fallback bitmap glyphs
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        ghostscript \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-deu \
        qpdf \
        unpaper \
        pngquant \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# pypdf      → split/merge/rotate, metadata
# pdfplumber → text and table extraction with layout
# Pillow     → image handling for rasterised pages
# ocrmypdf   → adds a searchable text layer to a scanned PDF, in place.
#   Installed with pip rather than apt so it lands on this image's Python
#   (the apt package targets the distro interpreter), making it usable both
#   as the `ocrmypdf` CLI and as an importable module.
RUN pip install --no-cache-dir --break-system-packages \
        pypdf \
        pdfplumber \
        Pillow \
        ocrmypdf

# Hermes bind-mounts the project here; declaring it keeps the path present even
# when a container is started without the mount.
WORKDIR /workspace
