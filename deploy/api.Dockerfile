FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend
# 公司部署默认使用内网制品库；外部 CI 可通过 Docker build arg 覆盖为公网源。
ARG PIP_INDEX_URL=http://af.hikvision.com.cn/artifactory/api/pypi/pypi/simple
ARG PIP_TRUSTED_HOST=af.hikvision.com.cn
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" .

RUN useradd --create-home --uid 10001 appuser && mkdir -p /var/lib/camera-logs/data && chown -R appuser:appuser /var/lib/camera-logs
USER appuser
EXPOSE 18080
CMD ["sh", "-ec", "exec uvicorn camera_logs.main:app --host 0.0.0.0 --port ${API_PORT:-18080}"]
