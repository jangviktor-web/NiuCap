#!/usr/bin/env bash
# ============================================================================
# 牛来选股面板 · 一键启动
#
# 用法：
#   ./deploy.sh            启动服务（后台常驻）
#   ./deploy.sh tunnel     启动服务 + Cloudflare 快速隧道（生成公网地址）
#   ./deploy.sh db         检查当前数据库后端是否连通
#   ./deploy.sh stop       停止全部
#
# 数据库（两种，二选一）：
#   SQLite  默认。数据存在 ./data/tick.db，重启不丢，不需要网络。
#   TiDB    复制 .env.example 为 .env 并填好 TICK_DB_HOST 等变量即自动切换。
#           多人共享同一份数据、手机/朋友直接访问时用它，此时可不用隧道。
#
# 切回本地：把 .env 改名或删掉 TICK_DB_HOST 即可，本地数据一直在。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PORT="${PORT:-8899}"
PIDDIR="$ROOT/.run"
VENV="$ROOT/.venv"
LOG="$ROOT/.run/server.log"

mkdir -p "$PIDDIR" "$ROOT/data"

# ---------------------------------------------------------------- 工具函数
say()  { printf "\033[1;36m▸\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 环境加载
# .env 里写数据库凭据（TICK_DB_HOST 等），存在则自动生效。
# 想切回本地 SQLite：把 .env 里的 TICK_DB_HOST 注释掉，或直接删掉 .env。
load_env() {
  if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
  fi
  if [ -n "${TICK_DB_HOST:-}" ]; then
    say "数据库后端：云端 MySQL 协议库 ${TICK_DB_HOST}:${TICK_DB_PORT:-4000}/${TICK_DB_NAME:-tick}"
  else
    say "数据库后端：本地 SQLite $ROOT/data/tick.db"
  fi
}

check_db() {
  "$1" - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd() + "/server")
try:
    import store
except Exception as e:
    print("  加载 store 失败:", e); sys.exit(1)
print("  BACKEND =", store.BACKEND)
try:
    info = store.initialize()
    users = store._conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    print("  目标    :", info["db"])
    print("  schema  :", info["schema"])
    print("  账号数  :", users)
    print("  连通性  : OK")
except Exception as e:
    print("  连通性  : 失败 ->", f"{type(e).__name__}: {e}")
    sys.exit(1)
PY
}

# ---------------------------------------------------------------- 依赖准备
ensure_python() {
  command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装 Python 3.9+"

  if [ ! -x "$VENV/bin/python" ]; then
    say "创建虚拟环境 $VENV"
    python3 -m venv "$VENV" || die "创建虚拟环境失败"
  fi
  if ! "$VENV/bin/python" -c "import fastapi, uvicorn, pandas" 2>/dev/null; then
    say "安装依赖（首次会比较慢）"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q -r requirements.txt || die "依赖安装失败"
  fi
  # 云端模式额外需要 pymysql（SQLite 模式用不到，放在这里按需装）
  if [ -n "${TICK_DB_HOST:-}" ] && ! "$VENV/bin/python" -c "import pymysql" 2>/dev/null; then
    say "安装云数据库依赖 pymysql"
    "$VENV/bin/pip" install -q pymysql || die "pymysql 安装失败"
  fi
  say "Python 环境就绪"
}

# ---------------------------------------------------------------- 服务管理
# 按监听端口反查真实 PID（最可靠，不依赖 pid 文件是否记对）
# 注意：找不到时返回空字符串且退出码 0，避免 set -e 误杀脚本
port_pid() {
  command -v ss >/dev/null 2>&1 || { echo ""; return 0; }
  ss -ltnp 2>/dev/null | grep -F ":$PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true
}

# 健康检查通过 => 服务确实在跑（比 kill -0 更可信）
server_up() {
  curl -sf -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1
}

server_running() {
  server_up && return 0
  [ -f "$PIDDIR/server.pid" ] && kill -0 "$(cat "$PIDDIR/server.pid")" 2>/dev/null
}

start_server() {
  if server_running; then
    say "服务已在运行（PID $(port_pid)）"
    return
  fi
  # 端口被残留进程占用（pid 文件已失效）：先清掉，否则启动会静默失败
  local stale; stale="$(port_pid)"
  if [ -n "$stale" ]; then
    warn "端口 $PORT 被残留进程 $stale 占用，先停止"
    kill "$stale" 2>/dev/null || true
    sleep 1
    kill -9 "$stale" 2>/dev/null || true
  fi

  say "启动服务，端口 $PORT"
  cd "$ROOT/server" || return 1
  nohup "$VENV/bin/python" run.py "$PORT" > "$LOG" 2>&1 &
  local srv_pid=$!
  cd "$ROOT" || return 1
  # 优先记录真实监听 PID，端口还没起时退回 nohup 的 PID
  local real=""
  for _ in 1 2 3 4 5; do
    sleep 1
    real="$(port_pid)"
    [ -n "$real" ] && break
  done
  echo "${real:-$srv_pid}" > "$PIDDIR/server.pid"

  # 等待健康检查通过（行情预热需要几秒）
  local i=0
  while [ $i -lt 30 ]; do
    if server_up; then
      say "服务已就绪 → http://127.0.0.1:$PORT"
      return
    fi
    i=$((i + 1)); sleep 1
  done
  warn "健康检查超时，请查看日志：$LOG"
  tail -20 "$LOG" || true
}

stop_all() {
  local stopped=0
  for f in "$PIDDIR"/*.pid; do
    [ -e "$f" ] || continue
    local pid; pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      say "已停止 PID $pid（$(basename "$f")）"
      stopped=1
    fi
    rm -f "$f"
  done
  # 兜底：pid 文件记错时，按端口清掉真实监听进程
  local lp; lp="$(port_pid)"
  if [ -n "$lp" ]; then
    kill "$lp" 2>/dev/null || true
    sleep 1
    if [ -n "$(port_pid)" ]; then kill -9 "$lp" 2>/dev/null || true; fi
    say "已停止占用端口 $PORT 的进程（PID $lp）"
    stopped=1
  fi
  if [ $stopped -eq 0 ]; then say "没有正在运行的服务"; fi
  return 0
}

# ---------------------------------------------------------------- 隧道
ensure_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    return
  fi
  say "未检测到 cloudflared，尝试自动安装"

  local os arch url bin
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) die "不支持的架构：$arch，请手动安装 cloudflared" ;;
  esac

  case "$os" in
    linux)
      bin="$ROOT/cloudflared"
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
      ;;
    darwin)
      if command -v brew >/dev/null 2>&1; then
        say "通过 Homebrew 安装 cloudflared"
        brew install cloudflared || die "brew 安装失败，请手动安装 cloudflared"
        return
      fi
      bin="$ROOT/cloudflared"
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$arch.tgz"
      ;;
    *) die "不支持的系统：$os，请手动安装 cloudflared" ;;
  esac

  if [[ "$url" == *.tgz ]]; then
    say "下载：$url"
    curl -fL --progress-bar "$url" -o "$ROOT/cloudflared.tgz" || die "下载失败"
    tar -xzf "$ROOT/cloudflared.tgz" -C "$ROOT"
    rm -f "$ROOT/cloudflared.tgz"
  else
    say "下载：$url"
    curl -fL --progress-bar "$url" -o "$bin" || die "下载失败（网络不通？可手动下载后放到 $ROOT/cloudflared）"
    chmod +x "$bin"
  fi
  say "cloudflared 就绪"
}

start_tunnel() {
  ensure_cloudflared
  local cfd
  cfd="$(command -v cloudflared || echo "$ROOT/cloudflared")"

  say "建立隧道（快速模式，免登录）"
  say "公网地址会在下面几行里出现，形如 https://xxxx.trycloudflare.com"
  echo "────────────────────────────────────────────────────────"

  # 前台运行，方便你直接看到地址；Ctrl+C 结束隧道
  "$cfd" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" 2>&1 | tee "$PIDDIR/tunnel.log"
}

# ---------------------------------------------------------------- 主流程
# 先加载 .env，后续所有步骤（包括依赖安装）都要依据它判断后端
load_env

case "${1:-start}" in
  stop)
    stop_all
    ;;
  db)
    ensure_python
    check_db "$VENV/bin/python"
    ;;
  tunnel)
    ensure_python
    start_server
    echo
    start_tunnel
    ;;
  start|"")
    ensure_python
    start_server
    echo
    say "打开浏览器访问：http://127.0.0.1:$PORT"
    if [ -n "${TICK_DB_HOST:-}" ]; then
      say "数据存放在云端数据库，多台设备访问同一份数据，换机/重启都不丢"
    else
      say "需要外网访问请执行：./deploy.sh tunnel"
    fi
    say "停止服务请执行：    ./deploy.sh stop"
    ;;
  *)
    echo "用法：$0 [start|tunnel|db|stop]"
    exit 1
    ;;
esac
