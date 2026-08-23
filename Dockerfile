# Deterministic build for Railway (and any container host).
#
# This replaced a Nixpacks build, which failed for two reasons worth
# remembering: the Nix Python ships without pip of its own, and Nixpacks
# copies only the dependency manifest before its `install` phase -- so
# `pip install .` ran before src/ and README.md existed.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Everything the build backend needs, and nothing else: pyproject declares
# README.md as the long description, and the package itself lives in src/.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install . \
    && mkdir -p /app/data \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app

USER app

# Railway injects PORT and overrides these with the service variables you set;
# the defaults just make `docker run -p 8000:8000 <image>` work on its own.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATABASE_URL=sqlite:////app/data/pokearb.db

EXPOSE 8000

CMD ["pokearb", "serve"]
