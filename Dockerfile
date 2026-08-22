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
        --no-deps \
        fastapi 'uvicorn[standard]' 'pydantic>=2.0,<3.0' 'pyyaml>=6.0,<7.0' \
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
        scikit-learn scipy joblib threadpoolctl \
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
        shellingham colorama
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
        unixodbc \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /var/lib/sophia/pycurity /tmp /home/sophia \
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
