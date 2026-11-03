# syntax=docker/dockerfile:1
# Seestar Sync as a Home Assistant add-on. Build context is the REPOSITORY ROOT
# (CI passes `file: addon/Dockerfile`), so the same source tree that produces the
# plain Docker/Kubernetes image produces this one.
#
# The HA base image is multi-arch and brings s6-overlay v3 and bashio. Debian
# (glibc) matches ha_seestar_alp so both add-ons share a base layer.
FROM ghcr.io/home-assistant/base-debian:trixie-2026.10.3

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# uv (static binary, multi-arch) installs the locked dependencies into a venv.
# Debian's Python is PEP 668 externally-managed, so nothing goes in the system
# site-packages; the venv bin dir goes first on PATH, which s6 (with-contenv)
# passes on to the service scripts.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable
ENV PATH="/opt/venv/bin:${PATH}"

# s6 services (rootfs overlay).
COPY addon/rootfs /
