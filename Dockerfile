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

# SB-stack deps (kahndor, subtrix, pycurity, ladyol) are local
# namespace packages — not on PyPI. They are NOT in this Dockerfile;
# instead, the build script `scripts/build-sophia.sh` vendors them
# into `sb-dist/` as wheels (using `pip wheel --no-deps`) before
# invoking `docker build`. The wheels are then installed as part of
# this Dockerfile step.
#
# If sb-dist/ is missing, the build fails loudly — operator should
# run `scripts/build-sophia.sh` (or copy wheels from a known-good
# CI artefact) before retrying.
COPY sb-dist/*.whl /tmp/sb-dist/
RUN pip install --prefix=/install --no-deps /tmp/sb-dist/*.whl \
    && rm -rf /tmp/sb-dist

# PyPI deps (fastapi, uvicorn, sse-starlette, httpx, pyyaml) resolve
# cleanly now that the SB-stack namespace packages are installed.
RUN pip install --prefix=/install --no-cache-dir .

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

EXPOSE 8765

# Health check — /healthz returns 200 as long as the FastAPI process
# is alive; does NOT touch the keystore, so a transient vault hiccup
# doesn't trigger a container restart loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8765/healthz || exit 1

# Production entrypoint: uvicorn.
CMD ["uvicorn", "sophia.server:app", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--log-level", "info"]
