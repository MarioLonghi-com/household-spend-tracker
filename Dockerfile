# syntax=docker/dockerfile:1.7
#
# The headline way to run this, and the one that makes the upgrade story
# trivial: the data cannot be inside the thing being replaced, because the
# thing being replaced is an image.
#
# Multi-stage. Node builds the client and is then thrown away -- the runtime
# has no Node, no npm and no node_modules in it, which is the largest single
# thing this removes from a deployment's attack surface and from its size.
#
# ## The base image, and the bypass
#
# Default is Chainguard's Wolfi-based Python. Minimal, rebuilt continuously,
# and the runtime variant carries **no shell and no package manager**, which
# removes most of what a container escape reaches for. Assessed rather than
# assumed, and two things about the free tier matter:
#
#   - It is `:latest` only. Versioned and historical tags are a paid
#     subscription, so a build that names `:latest` alone is not reproducible
#     -- the tag moves underneath it. So both bases are pinned by **digest**
#     below, and Dependabot's `docker` entry moves the digests, the same
#     discipline as the SHA-pinned actions in .github/workflows/ (#95).
#   - Chainguard Libraries for Python -- their rebuilt-from-source PyPI
#     packages -- is a **paid** product and is not in use. Nothing here should
#     be read as claiming otherwise.
#
# The bypass the review asked for is a build argument, and CI builds both so it
# is not a promise:
#
#     docker build --build-arg PY_BASE=python:3.12-slim \
#                  --build-arg PY_RUN=python:3.12-slim .
#
# The defaults name the two stages just below rather than the images, because
# Dependabot rewrites literal `FROM` lines and does not follow a build argument
# into one. A stage that nothing builds from is skipped, so the bypass never
# pulls a Chainguard image.
ARG PY_BASE=chainguard-dev
ARG PY_RUN=chainguard-run

# --------------------------------------------------------------------------- #
# **Pinned by digest** (#95): the multi-arch index (amd64, arm64) of
# `cgr.dev/chainguard/python:latest-dev` and `:latest` as of 2026-10-07, read
# from the registry's manifest endpoint and checked against the SHA-256 of the
# index itself. The tag stays beside the digest so Dependabot knows what to
# look up; the digest is what is pulled.
FROM cgr.dev/chainguard/python:latest-dev@sha256:630df1be3733f7b38d1b535872904248adfe23fbea4befcb08da47cb7436ddb2 AS chainguard-dev
FROM cgr.dev/chainguard/python:latest@sha256:8c6e0d0a587455e8a8d145e20234d5ef5a531a1c052a7b9d76b155ccc7fcded2 AS chainguard-run

# --------------------------------------------------------------------------- #
# **Pinned by digest.** This stage builds the JavaScript that ships, so a tag
# that moved underneath it would change the product without a commit saying
# so. The digest is the multi-arch index for `node:22-alpine` as of 2026-09-24,
# read with `docker buildx imagetools inspect node:22-alpine` and matched
# against the Docker Hub API. Dependabot's `docker` entry moves it.
#
# A literal `FROM` rather than an `ARG` default, because Dependabot rewrites
# `FROM` lines and does not follow a build argument into one -- which is also
# why the two Chainguard bases above are stages of their own.
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS client
# The layout matters: `client/vite.config.ts` has `outDir: "../app/static/dist"`,
# so the build writes *outside* the client directory and the stage has to give
# it somewhere to land. /src/client and /src/app mirror the repository.
WORKDIR /src/client
# The lockfile alone first, so a change to a source file does not re-run the
# install. `npm ci`, never `npm install`: ci installs exactly the lockfile and
# fails if package.json disagrees with it, which is the property that makes a
# build reproducible.
COPY client/package.json client/package-lock.json ./
RUN npm ci
COPY client/ ./
RUN npm run build && test -f /src/app/static/dist/index.html

# --------------------------------------------------------------------------- #
FROM ${PY_BASE} AS deps
# **root, and only in this stage.** Chainguard's `-dev` variant runs as the
# nonroot user (uid 65532) like its runtime sibling, so `python -m venv /venv`
# fails with `[Errno 13] Permission denied: '/venv'` -- / is not writable by
# that user. This stage is discarded; nothing it runs as reaches the image.
#
# Building the venv somewhere nonroot *can* write is not the alternative it
# looks like: a venv bakes its own absolute path into `pyvenv.cfg` and into
# every console script's shebang, so one built at `/home/nonroot/venv` and
# copied to `/venv` is subtly broken. The path has to be the final one, and the
# final one is at the root. Under the `PY_BASE=python:3.12-slim` bypass this
# line is a no-op, which is the other reason to do it here rather than pick a
# different directory per base.
USER root
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt ./
# Into a virtualenv rather than the system site-packages, so the runtime stage
# copies one directory and inherits nothing else from the builder. From the
# hashed lock, so the image holds exactly the locked set and a download that
# does not match its hash fails the build (#46).
RUN python -m venv /venv && /venv/bin/pip install --require-hashes -r requirements.txt

# The data directory, created here because the runtime image has **no shell**
# and cannot `RUN mkdir` -- and it has to exist *in the image*, owned by the
# user the app runs as.
#
# Docker initialises a fresh named volume from whatever is at that path in the
# image, **ownership included**. If the path does not exist, it creates it
# owned by root, the app runs as 65532, and the first boot dies trying to write
# `secret.key`. The `.keep` file is not decoration: it guarantees the directory
# survives the COPY as a directory rather than depending on how an empty one is
# treated.
RUN mkdir -p /var/lib/spend-tracker && touch /var/lib/spend-tracker/.keep

# --------------------------------------------------------------------------- #
FROM ${PY_RUN} AS runtime
# OCI annotations. `source` is what links a published image back to this
# repository on a registry page; the registry path itself must be lowercase.
LABEL org.opencontainers.image.title="Spend Tracker" \
      org.opencontainers.image.description="A self-hosted, multi-currency spend tracker for one household" \
      org.opencontainers.image.source="https://github.com/MarioLonghi-com/household-spend-tracker" \
      org.opencontainers.image.url="https://github.com/MarioLonghi-com/household-spend-tracker" \
      org.opencontainers.image.documentation="https://github.com/MarioLonghi-com/household-spend-tracker/blob/main/deploy/DOCKER.md" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
WORKDIR /app
ENV PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPENDTRACKER_DATA_DIR=/var/lib/spend-tracker

COPY --from=deps /venv /venv
# Owned by the user that will run, so the fresh volume is too. See the deps
# stage for why this is not a `RUN mkdir` here.
COPY --from=deps --chown=65532:65532 /var/lib/spend-tracker /var/lib/spend-tracker
COPY app/ ./app/
COPY statements/ ./statements/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/
COPY alembic.ini pyproject.toml ./
COPY --from=client /src/app/static/dist ./app/static/dist
COPY deploy/entrypoint.py ./deploy/entrypoint.py

# The data is a volume by construction, which is finding #5 of the production
# plan solved rather than documented: `git clean -xdf` cannot reach it because
# it is not in a working tree.
VOLUME ["/var/lib/spend-tracker"]

# Loopback inside the container is wrong -- nothing could reach it -- but the
# *published* port in compose.yaml is 127.0.0.1, so the exposure boundary is
# still the host's loopback and `tailscale serve` still terminates the TLS.
EXPOSE 8848

# Chainguard's runtime image already runs as uid 65532. Named anyway so the
# `--build-arg PY_RUN=python:3.12-slim` bypass does not quietly run as root.
USER 65532:65532

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8848/api/health', timeout=4).status==200 else 1)"]

ENTRYPOINT ["python", "deploy/entrypoint.py"]
