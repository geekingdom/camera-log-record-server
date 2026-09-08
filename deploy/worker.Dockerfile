FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install --no-cache-dir .

COPY deploy/worker-entrypoint.sh /usr/local/bin/worker-entrypoint
RUN chmod 755 /usr/local/bin/worker-entrypoint && useradd --create-home --uid 10001 appuser && mkdir -p /var/lib/camera-logs/data && chown -R appuser:appuser /var/lib/camera-logs
USER appuser
CMD ["worker-entrypoint"]
