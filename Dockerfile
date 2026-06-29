# syntax=docker/dockerfile:1

ARG NODE_MAJOR=22
ARG CLAUDE_CLI_VERSION=1.0.33
ARG POETRY_VERSION=2.1.3

# ---------------------------------------------------------------------------
# Base image: Python 3.12 slim on Debian Bookworm
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

ARG NODE_MAJOR
ARG CLAUDE_CLI_VERSION
ARG POETRY_VERSION

# ---------------------------------------------------------------------------
# Layer 1: System packages + Node.js from NodeSource
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        ripgrep \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Layer 2: Claude Code CLI (pinned version)
# ---------------------------------------------------------------------------
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}

# ---------------------------------------------------------------------------
# Layer 3: Poetry
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir poetry==${POETRY_VERSION}

# ---------------------------------------------------------------------------
# Layer 4: Create non-privileged user and directories
# ---------------------------------------------------------------------------
RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /bin/bash bot \
    && mkdir -p /app /work \
    && chown bot:bot /app /work

WORKDIR /app

ENV APPROVED_DIRECTORY=/work \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

# ---------------------------------------------------------------------------
# Layer 5: Python dependencies (cached unless pyproject/lock change)
# ---------------------------------------------------------------------------
COPY --chown=bot:bot pyproject.toml poetry.lock ./

RUN poetry install --only main --extras voice_groq --no-root

# ---------------------------------------------------------------------------
# Layer 6: Application source
# ---------------------------------------------------------------------------
COPY --chown=bot:bot src/ ./src/

RUN pip install --no-cache-dir --no-build-isolation .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
USER bot

CMD ["claude-telegram-bot"]
