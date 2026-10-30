# Crypto Liquidity Engine — PHASE 1-2
# Multi-stage build: Python backend + Vite frontend

# ---- Build frontend ----
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Build backend ----
FROM python:3.13-slim AS backend-build
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY backend/ ./backend/
RUN uv pip install --system --no-cache .

# ---- Runtime ----
FROM python:3.13-slim
WORKDIR /app

# Установка системных CA (truststore использует системное хранилище)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Копируем установленные пакеты
COPY --from=backend-build /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=backend-build /usr/local/bin /usr/local/bin

# Копируем код
COPY backend/ ./backend/
COPY configs/ ./configs/
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Директория для данных
RUN mkdir -p /app/datasets/raw /app/datasets/derived

ENV PYTHONPATH=/app/backend
ENV CLE_CONFIG=configs/default.yaml
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]