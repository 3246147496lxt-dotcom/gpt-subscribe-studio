#!/usr/bin/env bash
# gpt-subscribe-studio 一键发布 / 回滚 / 状态检查脚本
#
# 用法：
#   ./scripts/deploy.sh                    # 标准发布：单测 + 脱敏扫描 + 前端构建 + 增量同步 + 薄层镜像发布
#   ./scripts/deploy.sh --backend-only     # 仅改后端时跳过前端 tsc/vite 构建
#   ./scripts/deploy.sh --skip-tests       # 跳过本地 pytest（仅紧急热修）
#   ./scripts/deploy.sh --rebuild-base     # 强制重建重依赖基础镜像 gss-api-base:py312
#   ./scripts/deploy.sh --dirty            # 允许工作区有未提交改动（默认拒绝，防漏提交）
#   ./scripts/deploy.sh --rollback [tag]   # 回滚到上一版（默认 gss-api:prev，或指定如 fa8a495）
#   ./scripts/deploy.sh --status           # 查看线上容器状态、镜像、磁盘余量与最近日志
#   ./scripts/deploy.sh --prune-old        # 安全清理过期构建缓存与多余旧版 gss-api 镜像（保留当前/prev/base/最近2个SHA）
#
# 目标主机配置（绝不写入仓库）：
#   放在 gitignored 的 config/deploy.local.env 中，或通过环境变量传入：
#     GSS_DEPLOY_HOST=...
#     GSS_DEPLOY_PORT=22
#     GSS_DEPLOY_USER=root
#     GSS_DEPLOY_DIR=/opt/gss/src
#     GSS_PUBLIC_URL=https://your-domain.example
#     GSS_DISK_MOUNT=/mnt/luoxue-data

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LOCAL_ENV_FILE="${GSS_DEPLOY_ENV_FILE:-$ROOT_DIR/config/deploy.local.env}"
if [[ -f "$LOCAL_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$LOCAL_ENV_FILE"
fi

DEPLOY_HOST="${GSS_DEPLOY_HOST:-}"
DEPLOY_PORT="${GSS_DEPLOY_PORT:-22}"
DEPLOY_USER="${GSS_DEPLOY_USER:-root}"
DEPLOY_DIR="${GSS_DEPLOY_DIR:-/opt/gss/src}"
PUBLIC_URL="${GSS_PUBLIC_URL:-}"
DISK_MOUNT="${GSS_DISK_MOUNT:-/mnt/luoxue-data}"
BASE_IMAGE="${GSS_BASE_IMAGE:-gss-api-base:py312}"

MODE="deploy"
BACKEND_ONLY=0
SKIP_TESTS=0
REBUILD_BASE=0
ALLOW_DIRTY=0
ROLLBACK_TAG="prev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-only) BACKEND_ONLY=1; shift ;;
    --skip-tests)   SKIP_TESTS=1; shift ;;
    --rebuild-base) REBUILD_BASE=1; shift ;;
    --dirty)        ALLOW_DIRTY=1; shift ;;
    --status)       MODE="status"; shift ;;
    --prune-old)    MODE="prune"; shift ;;
    --rollback)
      MODE="rollback"
      shift
      if [[ $# -gt 0 && "$1" != --* ]]; then
        ROLLBACK_TAG="$1"
        shift
      fi
      ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "[deploy] 未知参数：$1（使用 --help 查看用法）" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$DEPLOY_HOST" ]]; then
  cat >&2 <<EOF
[deploy] 缺少目标主机配置 GSS_DEPLOY_HOST。
请在 gitignored 的 $LOCAL_ENV_FILE 中配置（chmod 600）：
  GSS_DEPLOY_HOST=your-server-ip-or-host
  GSS_DEPLOY_PORT=22
  GSS_DEPLOY_USER=root
  GSS_DEPLOY_DIR=/opt/gss/src
  GSS_PUBLIC_URL=https://your-domain.example
EOF
  exit 2
fi

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -p "$DEPLOY_PORT")
SSH_TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"

log() {
  printf '\n\033[1;36m[deploy]\033[0m %s\n' "$*"
}

die() {
  printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

run_ssh() {
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"
}

if [[ "$MODE" == "status" ]]; then
  log "查询线上状态（${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PORT}）"
  run_ssh "set -e
    echo '=== 容器状态 ==='
    docker ps -a --filter 'name=^/gss-api$' --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
    echo
    echo '=== gss-api 镜像 ==='
    docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedSince}}' | grep -E 'REPOSITORY|gss-api'
    echo
    echo '=== 磁盘余量 ==='
    df -h '${DISK_MOUNT}' / 2>/dev/null || df -h /
    echo
    echo '=== 本地健康探活 ==='
    curl -fsS -o /dev/null -w 'http://127.0.0.1:18120/ -> HTTP %{http_code}\n' -L http://127.0.0.1:18120/
    echo
    echo '=== 最近 20 行日志 ==='
    docker logs --tail 20 gss-api 2>&1 || true"
  if [[ -n "$PUBLIC_URL" ]]; then
    curl -fsS -o /dev/null -w "公网 ${PUBLIC_URL}/ -> HTTP %{http_code}\n" -L "${PUBLIC_URL}/"
  fi
  exit 0
fi

if [[ "$MODE" == "rollback" ]]; then
  log "回滚线上 gss-api 到镜像标签 gss-api:${ROLLBACK_TAG}"
  run_ssh "set -e
    docker image inspect 'gss-api:${ROLLBACK_TAG}' >/dev/null 2>&1 || { echo '目标镜像 gss-api:${ROLLBACK_TAG} 不存在' >&2; exit 1; }
    docker tag 'gss-api:${ROLLBACK_TAG}' gss-api:pureproto
    cd '${DEPLOY_DIR}'
    docker compose -f deploy/docker/docker-compose.yml up -d --no-build --force-recreate
    for i in \$(seq 1 20); do
      st=\$(docker inspect -f '{{.State.Health.Status}}' gss-api 2>/dev/null || echo starting)
      if [ \"\$st\" = 'healthy' ]; then break; fi
      sleep 2
    done
    docker ps --filter 'name=^/gss-api$' --format '{{.Names}} | {{.Image}} | {{.Status}}'
    curl -fsS -o /dev/null -w 'local_http=%{http_code}\n' -L http://127.0.0.1:18120/"
  if [[ -n "$PUBLIC_URL" ]]; then
    curl -fsS -o /dev/null -w "public_https=%{http_code}\n" -L "${PUBLIC_URL}/"
  fi
  log "回滚完成。"
  exit 0
fi

if [[ "$MODE" == "prune" ]]; then
  log "清理线上构建缓存与多余旧版 gss-api 镜像（保留当前运行镜像、pureproto、prev、base 与最近 2 个 SHA）"
  run_ssh "set -e
    docker builder prune -af >/dev/null 2>&1 || true
    RUNNING_ID=\$(docker inspect -f '{{.Image}}' gss-api 2>/dev/null || true)
    KEEP_IDS=\"\$RUNNING_ID\"
    for tag in 'gss-api:pureproto' 'gss-api:prev' '${BASE_IMAGE}'; do
      id=\$(docker inspect -f '{{.Id}}' \"\$tag\" 2>/dev/null || true)
      KEEP_IDS=\"\$KEEP_IDS \$id\"
    done
    # 按创建时间保留最近 2 个非保留标签
    kept_extra=0
    while read -r ref id; do
      case \"\$ref\" in
        gss-api:pureproto|gss-api:prev|${BASE_IMAGE}) continue ;;
      esac
      if [ \$kept_extra -lt 2 ]; then
        kept_extra=\$((kept_extra + 1))
        continue
      fi
      if echo \"\$KEEP_IDS\" | grep -q \"\$id\"; then
        docker rmi \"\$ref\" >/dev/null 2>&1 || true
      else
        echo \"删除旧镜像: \$ref (\$id)\"
        docker rmi \"\$ref\" || true
      fi
    done < <(docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep '^gss-api:' || true)
    docker image prune -f >/dev/null 2>&1 || true
    df -h '${DISK_MOUNT}' 2>/dev/null | tail -n 1 || df -h / | tail -n 1"
  exit 0
fi

# ── 标准发布流程 ─────────────────────────────────────────────
if [[ "$ALLOW_DIRTY" -eq 0 ]]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "检测到未提交的代码改动。请先提交后再发布，或使用 --dirty 显式允许。"
  fi
fi

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
if [[ "$ALLOW_DIRTY" -eq 1 ]] && { ! git diff --quiet || ! git diff --cached --quiet; }; then
  GIT_SHA="${GIT_SHA}-dirty"
fi

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="python3"

log "1/5 执行敏感信息发布前扫描（release_check.py）"
"$PYTHON_BIN" scripts/release_check.py

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  log "2/5 运行本地单元测试（tests/unit）"
  if command -v uv >/dev/null 2>&1; then
    uv run --with pytest --with pytest-asyncio pytest tests/unit -q
  else
    "$PYTHON_BIN" -m pytest tests/unit -q
  fi
else
  log "2/5 已跳过本地单元测试（--skip-tests）"
fi

if [[ "$BACKEND_ONLY" -eq 0 ]]; then
  log "3/5 执行前端类型检查与生产构建（apps/web）"
  (
    cd apps/web
    ./node_modules/.bin/tsc --noEmit -p tsconfig.json
    ./node_modules/.bin/vite build
  )
else
  log "3/5 已跳过前端构建（--backend-only）"
fi

DEPS_HASH="$(cat deploy/docker/Dockerfile.base deploy/server/requirements.txt | shasum -a 256 | awk '{print substr($1,1,16)}')"
log "4/5 增量同步代码到 ${SSH_TARGET}:${DEPLOY_DIR}（commit=${GIT_SHA}, deps=${DEPS_HASH}）"

RSYNC_EXCLUDES=(
  --exclude '.git/'
  --exclude '.venv/'
  --exclude 'venv/'
  --exclude 'node_modules/'
  --exclude '.pnpm-store/'
  --exclude '.pytest_cache/'
  --exclude '.ruff_cache/'
  --exclude '.mypy_cache/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude 'antigravity-armorbreak/'
  --exclude 'data/'
  --exclude 'config/'
  --exclude 'runtime/'
  --exclude 'tmp/'
  --exclude 'attic/'
  --exclude '.env'
  --exclude '.env.*'
  --exclude 'docs/ops-hosts.md'
)

if [[ "$BACKEND_ONLY" -eq 1 ]]; then
  # 仅发后端时不删除远程已构建好的 apps/web/dist
  RSYNC_EXCLUDES+=(--exclude 'apps/web/dist/')
fi

rsync -az --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  "${RSYNC_EXCLUDES[@]}" \
  "$ROOT_DIR/" "${SSH_TARGET}:${DEPLOY_DIR}/"

log "5/5 远程构建薄层业务镜像、重启并健康校验"
run_ssh "set -e
  cd '${DEPLOY_DIR}'
  BASE_IMG='${BASE_IMAGE}'
  WANT_HASH='${DEPS_HASH}'
  FORCE_BASE='${REBUILD_BASE}'

  CUR_HASH=\$(docker inspect -f '{{index .Config.Labels \"gss.deps_hash\"}}' \"\$BASE_IMG\" 2>/dev/null || echo missing)
  if [ \"\$FORCE_BASE\" = '1' ] || [ \"\$CUR_HASH\" != \"\$WANT_HASH\" ]; then
    echo \"[remote] 需要构建基础镜像 \$BASE_IMG (current=\$CUR_HASH, want=\$WANT_HASH)\"
    docker builder prune -af >/dev/null 2>&1 || true
    FREE_MB=\$(df -Pm '${DISK_MOUNT}' 2>/dev/null | awk 'NR==2 {print \$4}' || echo 9999)
    if [ \"\${FREE_MB:-9999}\" -lt 2600 ]; then
      echo \"[remote] 磁盘余量不足（${DISK_MOUNT} 仅剩 \${FREE_MB}MB，重建基础镜像至少需 2600MB）。请先运行 ./scripts/deploy.sh --prune-old 清理旧镜像。\" >&2
      exit 1
    fi
    docker build -f deploy/docker/Dockerfile.base --build-arg DEPS_HASH=\"\$WANT_HASH\" -t \"\$BASE_IMG\" .
    docker builder prune -af >/dev/null 2>&1 || true
  else
    echo \"[remote] 命中基础镜像 \$BASE_IMG (deps_hash=\$CUR_HASH)，跳过重依赖构建\"
  fi

  if docker image inspect gss-api:pureproto >/dev/null 2>&1; then
    docker tag gss-api:pureproto gss-api:prev
  fi

  docker build -f deploy/docker/Dockerfile --build-arg BASE_IMAGE=\"\$BASE_IMG\" \
    -t gss-api:pureproto -t 'gss-api:${GIT_SHA}' .
  docker compose -f deploy/docker/docker-compose.yml up -d --no-build --force-recreate

  for i in \$(seq 1 20); do
    st=\$(docker inspect -f '{{.State.Health.Status}}' gss-api 2>/dev/null || echo starting)
    if [ \"\$st\" = 'healthy' ]; then break; fi
    sleep 2
  done

  docker ps --filter 'name=^/gss-api$' --format '{{.Names}} | {{.Image}} | {{.Status}}'
  curl -fsS -o /dev/null -w 'local_http=%{http_code}\n' -L http://127.0.0.1:18120/
  df -h '${DISK_MOUNT}' 2>/dev/null | tail -n 1 || df -h / | tail -n 1"

if [[ -n "$PUBLIC_URL" ]]; then
  curl -fsS -o /dev/null -w "public_https=%{http_code}\n" -L "${PUBLIC_URL}/"
fi

log "发布成功：gss-api:${GIT_SHA}（回滚可用 ./scripts/deploy.sh --rollback）"
