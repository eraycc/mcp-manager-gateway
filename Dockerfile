FROM python:3.11-slim-bullseye AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /usr/local/bin/
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN sed -i 's/requires-python = ">=3.12"/requires-python = ">=3.11"/' pyproject.toml \
    && uv lock --python 3.11 \
    && uv build --wheel \
    && uv export --frozen --no-dev --no-emit-project --output-file requirements.txt

FROM python:3.11-slim-bullseye AS base
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /usr/local/bin/
ENV PYTHONUNBUFFERED=1 MCP_MANAGER_HOME=/data HOST=0.0.0.0 PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=build /build/dist /wheels
COPY --from=build /build/requirements.txt /wheels/requirements.txt
RUN uv venv /opt/venv && uv pip install --python /opt/venv/bin/python -r /wheels/requirements.txt && uv pip install --python /opt/venv/bin/python --no-deps /wheels/*.whl
EXPOSE 8765
VOLUME ["/data"]
CMD ["mcp-manager", "serve"]

FROM build AS test
COPY tests ./tests
RUN uv sync --frozen --group dev
CMD ["uv", "run", "--no-sync", "pytest", "-q", "--ignore=tests/test_browser.py", "--ignore=tests/test_browser_workflows.py", "--ignore=tests/test_browser_imports.py", "--ignore=tests/test_browser_controls.py", "--ignore=tests/test_browser_bug1.py"]

FROM base AS production
