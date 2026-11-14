# 多阶段构建：先编前端，再把产物交给后端托管，最后一个容器跑整套
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# 中文字体（讲义 PDF / 课件打印需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./
COPY --from=web /web/dist ./frontend-dist

ENV FRONTEND_DIST_DIR=/app/frontend-dist \
    DATABASE_URL=sqlite:////data/coursegenie.db \
    STORAGE_DIR=/data/storage

VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
