FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/root

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm config set registry "${NPM_REGISTRY}" \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-mintimeout 20000 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm config set fetch-timeout 300000 \
    && npm install -g --no-audit --no-fund @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force

COPY pyproject.toml uv.lock README.md ./
COPY application ./application
COPY config ./config
COPY domain ./domain
COPY infrastructure ./infrastructure
COPY presentation ./presentation
COPY shared ./shared
COPY cli.py __main__.py ./

RUN uv sync --locked --no-dev \
    && mkdir -p /data /root/.claude /root/.codex

VOLUME ["/data", "/root/.claude", "/root/.codex"]

CMD ["uv", "run", "--no-sync", "ai-limit-monitor", "run"]
