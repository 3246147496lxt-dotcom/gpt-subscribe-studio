# 轻量业务应用镜像：基于 gss-api-base:py312 仅叠加代码与前端构建产物（约 2MB，秒级构建）
#
# 首次构建（或 requirements.txt 变更时）先构建底座：
#   docker build -f deploy/docker/Dockerfile.base -t gss-api-base:py312 .
#
# 日常发布直接使用 ./scripts/deploy.sh（会自动校验依赖哈希并在需要时重建底座）。

ARG BASE_IMAGE=gss-api-base:py312
FROM ${BASE_IMAGE}

COPY packages/ /app/packages/
COPY services/ /app/services/
COPY deploy/server/app.py /app/app.py
COPY deploy/server/login.html /app/login.html
COPY apps/web/dist/ /app/web/
