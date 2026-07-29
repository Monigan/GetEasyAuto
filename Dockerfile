FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml README.md main.py ./
COPY src ./src

RUN mkdir -p /data/images

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/meta', timeout=3)"

CMD ["python", "main.py", "--viewer", "--scheduler", \
     "--viewer-host", "0.0.0.0", "--viewer-port", "8080", \
     "--database", "/data/listings.db", "--cache-dir", "/data/images", \
     "--validation-interval", "60"]
