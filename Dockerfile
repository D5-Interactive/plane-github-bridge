FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

RUN useradd --create-home --uid 10001 bridge && mkdir /data && chown bridge /data
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
COPY bridge/ bridge/

USER bridge
ENV PATH="/app/.venv/bin:$PATH" BRIDGE_CONFIG=/app/config.yaml BRIDGE_DB=/data/bridge.db
EXPOSE 8095
CMD ["uvicorn", "bridge.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "8095", "--proxy-headers"]
