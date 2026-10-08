# syntax=docker/dockerfile:1

# The api image: FastAPI on uvicorn. The same image runs the migrations and the
# admin scripts (`alembic upgrade head`, `python create_user.py ...`).
#
# Two stages on the same base image, so the virtual environment built in the first
# points at an interpreter that exists in the second. The final image holds no uv,
# no build cache and no tests.

ARG PYTHON_IMAGE=python:3.14-slim

FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
# `UV_PYTHON` wins over `.python-version`, which pins a patch release the base
# image may not have; downloads stay off so the image's own Python is used.
ENV UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
# Dependencies first, in their own layer: code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .

FROM ${PYTHON_IMAGE}
# A fixed uid: on the server the PDF folder is owned by it (`chown 10001`).
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
# Owned by root and read-only for the app: it writes only to STORAGE_DIR and /tmp.
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
# Set by the build (`sha-<commit>`); health, logs and the UI report it.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
USER 10001
EXPOSE 8000
# The image has no curl.
HEALTHCHECK --interval=10s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]
# One process, no reload. `--proxy-headers` trusts the addresses in
# FORWARDED_ALLOW_IPS (set in compose to the internal subnet, where nginx lives),
# so the app sees the visitor's IP and https. The app writes its own JSON request
# lines, hence `--no-access-log`. 30 s for requests in flight when stopped.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "30"]
