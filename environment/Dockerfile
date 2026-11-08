FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils procps \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==8.3.3

WORKDIR /app
COPY app/ /app/
