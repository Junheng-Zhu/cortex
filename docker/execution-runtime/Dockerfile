FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git grep findutils \
    && python -m pip install --no-cache-dir pytest \
    && useradd --uid 1000 --create-home --shell /bin/bash cortex \
    && rm -rf /var/lib/apt/lists/*

USER cortex
WORKDIR /workspace
CMD ["sleep", "infinity"]
