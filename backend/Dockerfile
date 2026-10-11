FROM ghcr.io/astral-sh/uv:0.8-python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# Contexts and Tools (SPEC §11). Mount a persistent volume here.
RUN mkdir -p /data
ENV SIDEWIT_DATA_DIR=/data

EXPOSE 8000
CMD ["uvicorn", "sidewit.main:app", "--host", "0.0.0.0", "--port", "8000"]
