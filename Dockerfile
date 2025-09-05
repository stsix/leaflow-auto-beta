# 第一阶段：构建
FROM python:3.9-bullseye AS builder

WORKDIR /build

# 安装构建依赖
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装Python包到用户目录
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# 第二阶段：运行
FROM python:3.9-slim-bullseye

WORKDIR /app

# 安装运行时依赖（cryptography需要）
RUN apt-get update && apt-get install -y \
    libssl1.1 \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制Python包
COPY --from=builder /root/.local /root/.local

# 复制应用文件
COPY app.py .
RUN mkdir -p /app/data

# 确保Python能找到包
ENV PATH=/root/.local/bin:$PATH
ENV PORT=8181 \
    ADMIN_USERNAME=admin \
    ADMIN_PASSWORD=admin123 \
    JWT_SECRET_KEY=""

EXPOSE 8181

CMD ["python", "app.py"]
