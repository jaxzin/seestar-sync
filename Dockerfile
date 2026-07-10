FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# Deliberately no USER directive: the archive volume's ownership varies by
# host (Mac SMB mounts, NAS bind mounts). k8s deployments should set a
# securityContext/fsGroup instead. Documented in the README.
ENTRYPOINT ["seestar-sync"]
CMD ["daemon"]
