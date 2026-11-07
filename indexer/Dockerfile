# syntax=docker/dockerfile:1.7
# RAHBAR indexer: the Job's image and the only writer to Chroma (section 4.2).
#
# Build context: indexer/, plus two named contexts so the image can carry the fetch
# script and the CI fixtures without the university's PDFs ever entering the image:
#
#   docker build -t rahbar-indexer:<sha> \
#     --build-context corpus=corpus --build-context fixtures=eval/fixtures indexer
#
# The real documents are downloaded at Job start by an initContainer running
# /app/corpus/fetch.py into the corpus PVC (see docs/PIPELINE.md, "How documents
# reach the Job").

# --- build stage -------------------------------------------------------------
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

# --- runtime stage -----------------------------------------------------------
FROM python:3.12-slim-bookworm
# OCR needs system binaries, not only the pip wrappers: tesseract for the text,
# poppler-utils (pdftoppm, pdffonts) to rasterise scanned pages.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CORPUS_DIR=/corpus \
    CORPUS_RAW=/corpus
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rahbar
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY pipeline/ pipeline/
COPY --from=corpus fetch.py sources.yaml MANIFEST.md /app/corpus/
COPY --from=fixtures *.md /app/fixtures/
USER 10001
ENTRYPOINT ["python", "-m", "pipeline.run"]
