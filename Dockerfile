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
# SB-stack (kahndor, subtrix, pycurity, ladyol, goalt, mindot,
# squirl, thingery, knightsort, glain, xskillaber, metrologic) is
# local-only — not on PyPI. Wheels are pre-built ON THE HOST by
# scripts/build-sophia.sh (from the workspace checkouts, which mirror
# GitVein @gamma) and vendored into sb-dist/ before this build. The
# container never runs git: all clone/build work happens on the host
# where git works. (In-container git+file:// clones fail on git
# safe.directory + bind-mount case-folding + pip --filter=blob:none —
# see docs/DEPLOY.md troubleshooting and the Glain record; do not
# retry that path.)
#
# If sb-dist/ is missing, the build fails loudly — run
# scripts/build-sophia.sh (or --no-build for wheels only) first.
COPY sb-dist/*.whl /tmp/sb-dist/
RUN pip install --prefix=/install --no-deps /tmp/sb-dist/*.whl \
    && rm -rf /tmp/sb-dist

# Two-step install:
#   1) PyPI runtime deps for sophia itself (--no-deps so they don't
#      drag in anything not on this list — the SB stack has heavy ML
#      deps it declares that we don't actually need). The list covers
#      everything we hit at runtime when booting sophia/ladyol.
#
# We explicitly list the PyPI deps that the SB stack and sophia
# pull in transitively. Without this, the first uvicorn boot fails
# with a ModuleNotFoundError on `rich`, then `anyio`, then ...
# pinning them here once is cheaper than chasing each one down.
RUN pip install --no-cache-dir --no-build-isolation setuptools wheel hatchling
COPY pyproject.toml README.md ./
# Install PyPI deps with --no-deps.
RUN pip install --prefix=/install --no-cache-dir --no-build-isolation --no-deps \
        fastapi 'uvicorn[standard]' 'pydantic==2.13.4' 'pyyaml>=6.0,<7.0' \
        'sse-starlette>=2.0' httpx click anyio \
        rich pydantic-settings python-dateutil \
        tenacity python-multipart itsdangerous idna certifi h11 \
        sniffio typing-extensions httpcore \
        markdown-it-py pygments typing_inspection 'pandas<3.0' 'numpy<3.0' \
        starlette 'pydantic-core==2.46.4' \
        'annotated-types>=0.6' annotated-doc \
        pytz python-dateutil tzdata six jsonschema \
        networkx opencv-python-headless Pillow webp \
        cryptography cffi \
        scikit-learn scipy joblib threadpoolctl cloudpickle \
        narwhals black mypy_extensions pytokens packaging attrs jsonschema \
        tomli platformdirs

# pathspec + tomlkit are pre-installed into /usr/local at the hatchling
# step (hatchling requires pathspec>=0.10.1). The bulk pip install
# below sees "Requirement already satisfied" and skips them — leaving
# /install without them. Force-install these two into /install
# explicitly with --ignore-installed so they live alongside the rest
# of the runtime deps in /install → /usr/local in the runtime stage.
RUN pip install --prefix=/install --no-cache-dir --no-build-isolation --no-deps \
        --ignore-installed pathspec==1.1.1 tomlkit==0.15.1 packaging pytokens \
        diskcache Send2Trash ruamel.yaml pyodbc \
        referencing rpds-py jsonschema-specifications typer \
        shellingham colorama psycopg2-binary
# Install sophia itself with --no-deps.
RUN pip install --prefix=/install --no-cache-dir --no-build-isolation \
        --no-deps .[standard]

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
        unixodbc \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /var/lib/sophia/pycurity /tmp /home/sophia /var/log/sophia \
    # /var/log/sophia is the container mount point for the sophia_logs
    # named volume. Create it as root with 0777 so the sophia user (uid
    # 1001) can write to it. When Docker initialises an empty named volume
    # it copies the image's directory contents + permissions, so the volume
    # will also be root-owned with 0777 — sophia can still write because
    # 'other' has rwx. This avoids NFS root-squash permission issues that
    # block bind-mount approaches on this host's NFS-mounted EDN share.
    && chmod 777 /var/log/sophia \
    && chown -R sophia:sophia /app /home/sophia /tmp /var/lib/sophia

WORKDIR /app

# Drop in the entrypoint BEFORE the chown (so the file is root-owned
# and we can chmod it). The runtime stage then chowns the rest of /app
# to the sophia user.
COPY cluster/entrypoint.py /app/entrypoint.py
RUN chmod 755 /app/entrypoint.py

COPY --from=builder /install /usr/local
COPY --from=builder /build/sophia ./sophia
COPY --from=builder /build/pyproject.toml ./
RUN chown -R sophia:sophia /app

# Bundle providers.yaml + providers_accts.yaml under /etc/sophia so
# SOPHIA_PROVIDER_POOL can point at /etc/sophia/providers.yaml
# without a volume mount. These RUN steps execute as root BEFORE
# the USER switch below (otherwise mkdir /etc/sophia fails with
# "Permission denied" because sophia:sophia can't create /etc/*).
# Bundle agent/ directory + agent_accts.yaml under /etc/sophia.
# The directory layout is:
#   /etc/sophia/agent/                       ← agent_path (dir)
#     agent.yaml                             (documentation; not read by ladyol)
#     agent_accts.yaml                       ← what ladyol reads
#
# ladyol's _initialize_pool() looks for the accts file as:
#   kahndor.Instruct(join(agent_path, f"{name}_accts.yaml"))
# where name = Path(agent_path).name.replace('.', '').
# With agent_path='/etc/sophia/agent':
#   name = 'agent' (no dots to strip)
#   accts path = /etc/sophia/agent/agent_accts.yaml ← correct
RUN mkdir -p /etc/sophia/agent
COPY cluster/agent/agent.yaml /etc/sophia/agent/agent.yaml
COPY cluster/agent/agent_accts.yaml /etc/sophia/agent/agent_accts.yaml
RUN chmod 644 /etc/sophia/agent/agent.yaml /etc/sophia/agent/agent_accts.yaml

USER sophia:sophia
ENV HOME=/home/sophia
# Health check — /healthz returns 200 as long as the FastAPI process
# is alive; does NOT touch the keystore, so a transient vault hiccup
# doesn't trigger a container restart loop. We use urllib (stdlib)
# rather than wget — the runtime image doesn't ship wget, and adding
# it just for a healthcheck would inflate the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8765/healthz',timeout=2); sys.exit(0 if r.status==200 else 1)"

# Sophia's vault init (cluster/entrypoint.py) creates the
# PyKeyStore vault with a random passphrase + caches the temp
# key, so the runtime never hits a getpass prompt (which EOFErrors
# in non-TTY containers). When a real provider key is added via
# `manage_secrets set`, the operator types the master passphrase
# from /var/lib/sophia/pycurity/.master_passphrase into the temp
# prompt — see cluster/README.md §"Adding provider keys".
CMD ["python", "/app/entrypoint.py"]
