# abc. backend: Python (FastAPI) + Node (gmgn-cli для реальных данных GMGN).
# Railway использует этот Dockerfile автоматически вместо Railpack.
FROM python:3.13-slim

# Node 20 + gmgn-cli (данные GMGN OpenAPI; без GMGN_API_KEY приложение
# само откатится на DATA_SOURCE=dex или mock — см. aitrader/app.py load_env)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g gmgn-cli@1.3.9 \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY aitrader/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aitrader/ ./aitrader/
WORKDIR /app/aitrader

CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
