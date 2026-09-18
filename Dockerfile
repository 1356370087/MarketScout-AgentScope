FROM python:3.14-slim AS runtime
# 使用镜像自带 Python 3.14；禁止 uv 隐式下载另一套解释器。
ENV UV_PYTHON_DOWNLOADS=never PYTHONPATH=/app/src
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
# 共用 HTTP/工具入口尚有兼容模块，迁移期间显式安装桥接依赖。
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra legacy-bridge
# 与 publisher-worker 使用同一 uid，保证共享卷上 0600 的 Job/工件双向可读写。
RUN groupadd --system --gid 10001 insightforge \
    && useradd --system --uid 10001 --gid insightforge --no-create-home insightforge \
    && mkdir -p /data/runs /data/documents /home/insightforge \
    && chown -R insightforge:insightforge /data/runs /data/documents /home/insightforge
COPY config ./config
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HOME=/home/insightforge RUNS_DIR=/data/runs
EXPOSE 2024
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:2024/healthz', timeout=3)"
USER insightforge:insightforge
CMD ["uvicorn", "open_deep_research.server:app", "--host", "0.0.0.0", "--port", "2024", "--timeout-graceful-shutdown", "30"]
