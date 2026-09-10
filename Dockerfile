# SPDX-License-Identifier: AGPL-3.0-or-later
# Stage 1: Compile Go connection daemon
FROM golang:1.24-alpine AS builder

WORKDIR /build
COPY go.mod ./
COPY cmd/ ./cmd/
COPY internal/ ./internal/

RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /build/minibots-conn ./cmd/conn-daemon

# Stage 2: Runtime image
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UMASK=0027

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e . --no-deps

COPY --from=builder /build/minibots-conn /app/bin/minibots-conn
RUN chmod 755 /app/bin/minibots-conn

COPY . /app/

RUN mkdir -p /app/data /app/sideload /app/bin && \
    useradd -r -s /usr/sbin/nologin -d /app -u 1000 minibot && \
    chown -R minibot:minibot /app

USER minibot

VOLUME ["/app/data", "/app/sideload"]

CMD ["sh", "-c", "/app/bin/minibots-conn & python3 /app/main.py"]
