# ビルドコンテキストは repo のルート（compose.yaml の build.context）。
FROM python:3.12.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY portal/requirements.txt /app/portal/requirements.txt
RUN pip install --requirement /app/portal/requirements.txt

COPY infrabox /app/infrabox
COPY portal /app/portal

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["uvicorn", "--factory", "portal.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
