# Stage 1: Build Frontend
FROM node:20-alpine AS frontend-build
WORKDIR /app

COPY web/package*.json ./web/
RUN cd web && npm ci

COPY web/ ./web/
RUN cd web && npm run build

# Stage 2: Backend
FROM python:3.10-slim
WORKDIR /app

ARG BUILD_DATE=""
ARG BUILD_VERSION=""
ARG VCS_REF=""

LABEL org.opencontainers.image.created=$BUILD_DATE \
      org.opencontainers.image.version=$BUILD_VERSION \
      org.opencontainers.image.revision=$VCS_REF

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

# 修复 Debian 源 + 安装依赖（国内网络专用）
RUN sed -i "s@deb.debian.org@mirrors.aliyun.com@g" /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY server/ ./server/
COPY --from=frontend-build /app/web/dist ./web/dist

ARG DOWNLOAD_MODELS=0
RUN if [ "$DOWNLOAD_MODELS" = "1" ]; then \
      python -c "from server.util.CaptchaUtils import ensure_model_exists, MODEL_URLS; [ensure_model_exists(k,v) for k,v in MODEL_URLS.items()]" ; \
    fi

EXPOSE 8147

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8147"]
