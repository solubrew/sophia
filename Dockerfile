# syntax=docker/dockerfile:1.7
#
# Multi-stage Dockerfile for Sophia.
#
# Build stage compiles Python dependencies + the application package.
# Runtime stage copies the pre-built artefacts and runs uvicorn.
#
# Security hardening (Podman-portable, non-root-safe):
#   - Runs as the dedicated `sophia` user with a fixed numeric UID (1001)
#     so the bind-mount file ownership works portably across Docker and
#     rootless Podman (which maps container UIDs to high host UIDs).
#   - Pinch-points only installed in the builder stage; the runtime
#     image is minimal.
#   - HEALTHCHECK probes /healthz (a FastAPI-side endpoint that doesn't
#     touch the keystore, so a transient vault hiccup doesn't trigger
#     a container restart loop).
#
# Build:
#   docker build -f Dockerfile -t sophia-web:local .

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Runtime deps first (cache layer reuse).
# README.md is required by hatchling's metadata step (readme field in
# pyproject.toml). COPY it before the first pip install.
COPY pyproject.toml README.md ./

# SB-stack deps (kahndor, subtrix, pycurity, ladyol, ...) are local
# namespace packages — not on PyPI. They are NOT in this Dockerfile;
# instead, the build script `scripts/build-sophia.sh` (or, for the
# cluster, `shieldface sb wheels`) vendors them into `sb-dist/` as
# wheels (using `pip wheel --no-deps`) before invoking `docker build`.
# The wheels are installed along with sophia + PyPI deps in a single
# pip install (see below) — splitting this into separate installs
# breaks pip's resolver (it tries to fetch kahndor from PyPI where
# the public stub has the wrong version).
#
# If sb-dist/ is missing, the build fails loudly — operator should
# run `scripts/build-sophia.sh` (or copy wheels from a known-good
# CI artefact) before retrying.
COPY sb-dist/*.whl /tmp/sb-dist/

# Two-step install:
#   1) SB wheels (--no-deps so their strict pins don't fight in the
#      resolver). The /install prefix puts them in
#      /install/lib/python3.12/site-packages for the next step.
#   2) Sophia's PyPI deps + the . package. --no-deps on the . install
#      skips re-resolving kahndor/ladyol (they're already installed
#      in step 1). We use --no-build-isolation because the builder
#      already has setuptools + wheel + hatchling.
#
# We explicitly list the PyPI deps that the SB stack and sophia
# pull in transitively. Without this, the first uvicorn boot fails
# with a ModuleNotFoundError on `rich`, then `anyio`, then ...
# pinning them here once is cheaper than chasing each one down.
RUN pip install --no-cache-dir --no-build-isolation setuptools wheel hatchling
COPY pyproject.toml README.md ./
# Install SB wheels without dependencies first — the SB stack has
# internal version conflicts in its PyPI deps (glain wants
# rich==14.3.4, kahndor wants rich<14.0), so installing them all
# in one resolver pass fails. We resolve them after, picking the
# version that satisfies both.
RUN pip install --prefix=/install --no-cache-dir --no-build-isolation --no-deps /tmp/sb-dist/*.whl
# Generate a constraints file pinning the SB-stack deps to the
# versions installed in /install. Used by the . install step below
# to stop pip from reaching out to PyPI for `kahndor` (where the
# public stub has the wrong version).
COPY cluster/gen_constraints.py /tmp/gen_constraints.py
RUN python3 /tmp/gen_constraints.py
# Install PyPI deps with --no-deps — we don't want them to drag in
# anything not on this list (the SB stack has heavy ML deps it
# declares that we don't actually need). The list covers everything
# we hit at runtime when booting sophia/ladyol.
RUN pip install --prefix=/install --no-cache-dir --no-build-isolation \
        --constraint /install/sb-constraints.txt \
        --no-deps \
        fastapi 'uvicorn[standard]' 'pydantic>=2.0,<3.0' 'pyyaml>=6.0,<7.0' \
        'sse-starlette>=2.0' httpx click anyio \
        rich pydantic-settings python-dateutil \
        tenacity python-multipart itsdangerous idna certifi h11 \
        sniffio typing-extensions httpcore \
        markdown-it-py pygments typing_inspection 'pandas<3.0' 'numpy<3.0' \
        starlette 'pydantic-core==2.46.4' \
        'annotated-types>=0.6' annotated_doc \
        pytz python-dateutil tzdata six jsonschema \
        networkx opencv-python-headless
# Install sophia itself with --no-deps.
RUN pip install --prefix=/install --no-cache-dir --no-build-isolation \
        --no-deps .[standard]
RUN rm -rf /tmp/sb-dist

# Project package (editable, no deps — deps already installed above).
COPY sophia ./sophia
RUN pip install --prefix=/install --no-deps .

# ─────────────────────────────────────────────────────────────────────────
# Runtime stage — minimal, runs as non-root
# ─────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOPHIA_ALLOW_ENV_FALLBACK=0

# Create the non-root sophia user with a FIXED numeric UID (1001) so
# bind-mount file ownership is portable across Docker and rootless
# Podman. The system tools (groupadd / useradd) need to run as root
# in the runtime stage, but the resulting image drops privileges below.
RUN groupadd --system --gid 1001 sophia \
    && useradd  --system --uid 1001 --gid 1001 \
               --home /app --shell /usr/sbin/nologin sophia

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /var/lib/sophia/pycurity /tmp /home/sophia \
    && chown -R sophia:sophia /app /home/sophia /tmp /var/lib/sophia

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /build/sophia ./sophia
COPY --from=builder /build/pyproject.toml ./
RUN chown -R sophia:sophia /app

USER sophia:sophia
ENV HOME=/home/sophia

# Sophia state dir (sqlite DB + captures + cache). The cluster
# compose-mounts a named volume here; the standalone single-container
# setup just writes to /var/lib/sophia inside the image (created
# below).
RUN mkdir -p /var/lib/sophia && chown sophia:sophia /var/lib/sophia

EXPOSE 8765

# Health check — /healthz returns 200 as long as the FastAPI process
# is alive; does NOT touch the keystore, so a transient vault hiccup
# doesn't trigger a container restart loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8765/healthz || exit 1

# Production entrypoint: uvicorn. PORT env var is honoured so the
# cluster compose file can override the default 8765.
SHELL ["/bin/sh", "-c"]
CMD uvicorn sophia.server:app --host ${HOST:-0.0.0.0} --port ${PORT:-8765} --log-level ${LOG_LEVEL:-info}
