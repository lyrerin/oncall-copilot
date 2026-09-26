# Dockerfile —— 应用镜像构建
#
# 关键改动（相对 docs/03 §2 的示例）：依赖层不再用 `pip install -e .`。
# 原因：editable 安装要在构建时校验包目录是否存在，而依赖层里只有 pyproject.toml
#   （源码在下一层才 COPY 进来，见「先拷清单让缓存生效」），所以必然报
#   error: package directory 'app' does not exist
# 现在改成用 uv 只解析 pyproject 里的依赖，不构建项目本身 —— 改业务代码不会让依赖层失效。
# 代码靠 WORKDIR /app + PYTHONPATH=/app 直接 import，不依赖 editable 安装。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# --- 依赖层：只拷清单，改代码不会让这层缓存失效 ---
RUN pip install --upgrade pip uv
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

# --- 代码层 ---
COPY . .

# 非 root 运行（面试加分项）
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
