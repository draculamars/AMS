FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY asset_manager ./asset_manager
ENV ASSET_DATA_DIR=/data ASSET_BLOB_DIR=/archive PYTHONUNBUFFERED=1 ASSET_LOCAL_BOOTSTRAP=0
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)"
CMD ["python","-m","asset_manager","serve","--host","0.0.0.0","--port","8765"]
