# 神笔文学创作台 · 单容器镜像（默认只读挂载即可跑）
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SHENBI_AUTH=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -m -u 10001 shenbi \
    && mkdir -p /app/projects /app/secrets /app/data \
    && chown -R shenbi:shenbi /app
USER shenbi

EXPOSE 46821
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:46821/').read()" || exit 1

# 容器内监听 0.0.0.0；本地通过 localhost 访问（Host 白名单默认放行）。
# 局域网/反代访问请设置 SHENBI_TRUSTED_HOSTS=<域名或IP>。
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "46821"]
