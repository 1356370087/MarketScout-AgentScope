FROM python:3.14-slim AS runtime
# 使用镜像自带 Python 3.14；禁止 uv 隐式下载另一套解释器。
ENV UV_PYTHON_DOWNLOADS=never PYTHONPATH=/app/src
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
# 应用与工具入口仅安装原生依赖；LangSmith 评估 SDK 通过独立可选依赖启用。
# 当前项目通过 PYTHONPATH 从源码运行；先安装依赖，使源码更新复用依赖层。
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked uv sync --frozen --no-dev
# 与 publisher-worker 使用同一 uid，保证共享卷上 0600 的 Job/工件双向可读写。
RUN groupadd --system --gid 10001 marketscout \
    && useradd --system --uid 10001 --gid marketscout --no-create-home marketscout \
    && mkdir -p /data/runs /data/documents /data/platform-config /home/marketscout \
    && chown -R marketscout:marketscout /data/runs /data/documents /data/platform-config /home/marketscout
COPY src ./src
COPY alembic.ini ./
COPY config ./config
# 默认追踪库与运行工件共用可写目录，支持直接以非 root 用户启动镜像。
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HOME=/home/marketscout RUNS_DIR=/data/runs TRACE_STORE_PATH=/data/runs/traces.sqlite3
EXPOSE 2024
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:2024/healthz', timeout=3)"
USER marketscout:marketscout
CMD ["uvicorn", "open_deep_research.server:app", "--host", "0.0.0.0", "--port", "2024", "--timeout-graceful-shutdown", "30"]
