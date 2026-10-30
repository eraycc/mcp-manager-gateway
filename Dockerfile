ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6

FROM ${UV_IMAGE} AS uv-bin

# Build the fixed WAL engine separately; production carries no compiler.
FROM ${PYTHON_IMAGE} AS sqlite-build
COPY docker/debian.sources /etc/apt/sources.list.d/debian.sources
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install --no-install-recommends -y gcc libc6-dev curl ca-certificates openssl patchelf
WORKDIR /sqlite
RUN curl --fail --location --retry 2 https://www.sqlite.org/2026/sqlite-autoconf-3510300.tar.gz -o source.tar.gz \
    && tar -xzf source.tar.gz --strip-components=1 \
    && test "$(openssl dgst -sha3-256 sqlite3.c | cut -d ' ' -f2)" = "32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e" \
    && gcc -O2 -shared -fPIC -Wl,-soname,libsqlite3.so.0 \
       -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS4 -DSQLITE_ENABLE_FTS5 \
       -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_ENABLE_COLUMN_METADATA \
       -o libsqlite3.so.0 sqlite3.c -ldl -lpthread -lm \
    && patchelf --set-rpath /opt/mmg/sqlite /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so

FROM ${PYTHON_IMAGE} AS python-runtime
COPY --from=sqlite-build /sqlite/libsqlite3.so.0 /opt/mmg/sqlite/libsqlite3.so.0
COPY --from=sqlite-build /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so /usr/local/lib/python3.12/lib-dynload/
COPY --from=uv-bin /uv /uvx /usr/local/bin/
# The extension's private RPATH avoids changing downstream MCP environments.
RUN uv run --no-project --python /usr/local/bin/python python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3, 51, 3), sqlite3.sqlite_version"

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

FROM build AS test
COPY tests ./tests
RUN uv sync --frozen --group dev
CMD ["uv", "run", "--no-sync", "pytest", "-q"]

FROM test AS browser-test
RUN uv run --no-sync playwright install --with-deps chromium

FROM base AS production
