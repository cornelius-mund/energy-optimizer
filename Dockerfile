FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENERGY_OPTIMIZER_CONFIG=/app/config.yaml \
    ENERGY_OPTIMIZER_FRONTEND_DIRECTORY=/app/frontend

WORKDIR /app

# Third-party dependencies get their own layer, keyed only on pyproject.toml, so
# a source or frontend change reuses it instead of reinstalling every package.
COPY pyproject.toml ./
RUN python -c "import tomllib; print(*tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies'], sep=chr(10))" \
        > /tmp/requirements.txt \
    && pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY README.md ./
COPY src ./src
COPY frontend ./frontend
COPY config.example.yaml ./config.yaml

RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/provider-data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["python", "-m", "energy_optimizer"]
