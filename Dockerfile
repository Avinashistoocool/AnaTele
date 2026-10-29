# Web UI container. The desktop app is distributed separately (see docs/DEPLOYMENT.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TELEMETRY_DATA_DIR=/data \
    ANATELE_HOST=0.0.0.0 \
    ANATELE_PORT=8766 \
    MPLBACKEND=Agg

RUN useradd --create-home --uid 10001 anatele && mkdir /data && chown anatele /data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY telemetry_platform ./telemetry_platform
RUN pip install --no-cache-dir . && python -m telemetry_platform.simulator --out /app/simulations

USER anatele
VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"ANATELE_PORT\"]}/api/health')"
CMD ["anatele-web"]
