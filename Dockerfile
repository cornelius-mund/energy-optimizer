FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENERGY_OPTIMIZER_CONFIG=/app/config.yaml \
    ENERGY_OPTIMIZER_FRONTEND_DIRECTORY=/app/frontend

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY frontend ./frontend
COPY config.example.yaml ./config.yaml

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/provider-data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["python", "-m", "energy_optimizer"]
