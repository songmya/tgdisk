FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 supervisord 用于管理多进程
RUN apt-get update && apt-get install -y supervisor && rm -rf /var/lib/apt/lists/*

COPY . .

# 暴露 WebUI 和 WebDAV 端口
EXPOSE 8080 8081

# 创建数据目录
RUN mkdir -p data

# 复制 supervisord 配置文件
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
