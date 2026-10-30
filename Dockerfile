ARG PYTHON_IMAGE=python:3.13-alpine3.23
ARG TEST_PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6

FROM ${UV_IMAGE} AS uv-bin

# Use the SQLite shipped by the maintained Python image; no custom engine.
FROM ${PYTHON_IMAGE} AS python-runtime
COPY --from=uv-bin /uv /uvx /usr/local/bin/

FROM python-runtime AS build
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && uv build --wheel \
    && uv export --frozen --no-dev --no-emit-project --output-file requirements.txt

FROM python-runtime AS base
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 MCP_MANAGER_HOME=/data HOST=0.0.0.0 PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=build /build/dist /wheels
COPY --from=build /build/requirements.txt /wheels/requirements.txt
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python -r /wheels/requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-deps /wheels/*.whl
EXPOSE 8765
VOLUME ["/data"]
CMD ["mcp-manager", "serve"]

# Browser tooling requires glibc. Keep the test target on Debian, independently
# of the small Alpine production runtime.
FROM ${TEST_PYTHON_IMAGE} AS test
COPY --from=uv-bin /uv /uvx /usr/local/bin/
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY tests ./tests
RUN uv sync --frozen --group dev
CMD ["uv", "run", "--no-sync", "pytest", "-q"]

FROM test AS browser-test
RUN uv run --no-sync playwright install --with-deps chromium

FROM base AS production
