FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      poppler-utils tesseract-ocr tesseract-ocr-pol curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
RUN curl -fsSL "$SUPERCRONIC_URL" -o /usr/local/bin/supercronic && chmod +x /usr/local/bin/supercronic
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY crontab /app/crontab
# Raw files volume; on the host it must be owned by uid 65534 (nobody), see docs/OPERATIONS.md.
RUN mkdir -p /data/raw && chown nobody:nogroup /data/raw
USER nobody
CMD ["tie", "worker"]
