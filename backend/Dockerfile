FROM ghcr.io/astral-sh/uv:0.8-python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Non-root; Compose `user:` may override it with the data directory's owner.
ARG UID=1000 GID=1000
RUN groupadd --gid "$GID" sidewit \
    && useradd --uid "$UID" --gid "$GID" --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin sidewit \
    && mkdir -p /data \
    && chown "$UID:$GID" /data
ENV SIDEWIT_DATA_DIR=/data
USER $UID:$GID

EXPOSE 8000
CMD ["uvicorn", "sidewit.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
