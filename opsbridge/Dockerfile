# syntax=docker/dockerfile:1

# ---------- base: dependencies --------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 opsbridge \
    && mkdir -p /data && chown opsbridge:opsbridge /data

# ---------- test: runs the pytest suite (docker compose run --rm tests) -----------------------
FROM base AS test

COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt

COPY --chown=opsbridge:opsbridge pytest.ini ./
COPY --chown=opsbridge:opsbridge app ./app
COPY --chown=opsbridge:opsbridge scripts ./scripts
COPY --chown=opsbridge:opsbridge tests ./tests

USER opsbridge
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]

# ---------- runtime (default, last stage) -----------------------------------------------------
FROM base AS runtime

COPY --chown=opsbridge:opsbridge app ./app
COPY --chown=opsbridge:opsbridge scripts ./scripts

USER opsbridge

ENV DATABASE_URL=sqlite:////data/opsbridge.db
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Single process on purpose: the in-process worker + SQLite (see docs/architecture.md 3.9).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
