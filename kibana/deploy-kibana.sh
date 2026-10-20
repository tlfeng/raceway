#!/usr/bin/env bash
# ============================================================================
# deploy-kibana.sh —— 在压测机上起 Kibana 7.17 看板（对接 EasySearch 指标库）
#
# 拓扑（都在本机回环上，不对外暴露）：
#   Kibana 7.17.29  127.0.0.1:5601
#        │
#        ▼
#   协议适配 shim  127.0.0.1:9202  （Node，无外部依赖）
#        │  Basic auth + 产品校验改写 + 12 处 API 差异兜底
#        ▼
#   EasySearch 2.5.0 指标库  https://127.0.0.1:9237
#
# 为什么需要 shim：Kibana 7.17 的 ES 客户端做产品校验（version/tagline/build_flavor
#   + X-elastic-product 头），EasySearch 2.5.0 默认不满足 → Kibana 直接崩溃。
# 为什么不改指标库配置去开 api_compatibility：指标库是入库管道的目标，
#   不为了看板去动它的配置；改写放在代理侧，指标库零变更。
#
# 外部访问：Kibana 只绑回环，用 SSH 隧道看：
#   ssh -L 5601:127.0.0.1:5601 root@203.0.113.10
#   浏览器打开 http://127.0.0.1:5601/app/dashboards#/view/bench-overview
#
# 用法：
#   ./deploy-kibana.sh start|stop|status
# 环境变量：
#   ES_PORT=9237 ES_USER=admin ES_PASS=*** KB_PORT=5601 SHIM_PORT=9202
#   KB_ROOT=/testing/kibana-rootfs/usr/share/kibana
# ============================================================================
set -uo pipefail

KB_ROOT="${KB_ROOT:-/testing/kibana-rootfs/usr/share/kibana}"
KB_DATA="${KB_DATA:-/testing/kibana-data}"
KB_LOGS="${KB_LOGS:-/testing/kibana-logs}"
KB_PORT="${KB_PORT:-5601}"
SHIM_PORT="${SHIM_PORT:-9202}"
ES_HOST="${ES_HOST:-127.0.0.1}"
ES_PORT="${ES_PORT:-9237}"
ES_USER="${ES_USER:-admin}"
ES_PASS="${ES_PASS:-}"
SHIM_JS="${SHIM_JS:-$(cd "$(dirname "$0")" && pwd)/shim-7.17-es250.js}"
PID_SHIM=/tmp/es_shim.pid
PID_KB=/tmp/kibana.pid

log() { echo "[deploy-kibana] $*"; }
die() { echo "[deploy-kibana] ERROR: $*" >&2; exit 1; }

start_shim() {
  [ -f "$SHIM_JS" ] || die "shim 脚本不存在: $SHIM_JS"
  [ -x "$KB_ROOT/node/bin/node" ] || die "Kibana 自带 Node 不存在: $KB_ROOT/node/bin/node"
  if [ -f "$PID_SHIM" ] && kill -0 "$(cat $PID_SHIM)" 2>/dev/null; then
    log "shim 已在跑 (pid $(cat $PID_SHIM))"; return 0
  fi
  TARGET_HOST="$ES_HOST" TARGET_PORT="$ES_PORT" TARGET_USER="$ES_USER" TARGET_PASS="$ES_PASS" \
  FIX_NODES_INFO=1 FIX_MAPPINGS=1 \
  setsid nohup "$KB_ROOT/node/bin/node" "$SHIM_JS" "$SHIM_PORT" /tmp/es_shim.log \
    > /tmp/es_shim.out 2>&1 &
  echo $! > "$PID_SHIM"
  sleep 2
  kill -0 "$(cat $PID_SHIM)" 2>/dev/null || { cat /tmp/es_shim.out; die "shim 启动失败"; }
  log "shim 已启动 pid=$(cat $PID_SHIM) 127.0.0.1:$SHIM_PORT -> https://$ES_HOST:$ES_PORT"
}

start_kibana() {
  if [ -f "$PID_KB" ] && kill -0 "$(cat $PID_KB)" 2>/dev/null; then
    log "Kibana 已在跑 (pid $(cat $PID_KB))"; return 0
  fi
  mkdir -p "$KB_DATA" "$KB_LOGS"
  cat > "$KB_ROOT/config/kibana.yml" <<EOF
# 由 deploy-kibana.sh 生成 —— 对接本机 EasySearch 指标库（经协议适配 shim）
server.port: $KB_PORT
server.host: "127.0.0.1"
server.shutdownTimeout: "30s"
server.name: "bench-metric-store"
path.data: $KB_DATA
path.logs: $KB_LOGS
elasticsearch.hosts: ["http://127.0.0.1:$SHIM_PORT"]
elasticsearch.username: "$ES_USER"
elasticsearch.password: "$ES_PASS"
elasticsearch.requestTimeout: 60000
elasticsearch.pingTimeout: 60000
monitoring.ui.container.elasticsearch.enabled: true
logging.log.level: info
EOF
  chmod 600 "$KB_ROOT/config/kibana.yml"
  cd "$KB_ROOT" || die "进不去 $KB_ROOT"
  setsid nohup ./bin/kibana > /tmp/kibana.out 2>&1 &
  echo $! > "$PID_KB"
  log "Kibana 启动中 pid=$(cat $PID_KB)（日志 /tmp/kibana.out）"
}

wait_ready() {
  local n=0
  while [ $n -lt 90 ]; do
    if curl -s --max-time 5 "http://127.0.0.1:$KB_PORT/api/status" 2>/dev/null | grep -q '"state"'; then
      log "Kibana /api/status 就绪"
      curl -s --max-time 10 "http://127.0.0.1:$KB_PORT/api/status" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    s = d.get("status", {})
    print("  overall:", s.get("overall", {}).get("state"), " version:", s.get("version", {}).get("number"))
    for k, v in sorted(s.items()):
        st = v.get("state") if isinstance(v, dict) else None
        if st in ("red", "degraded"):
            print("  非绿:", k, st)
except Exception as e:
    print("  解析状态失败:", e)
' 2>/dev/null
      return 0
    fi
    n=$((n+1)); sleep 5
  done
  echo "--- Kibana 日志尾部 ---"; tail -30 /tmp/kibana.out
  die "Kibana 未在 450s 内就绪"
}

case "${1:-start}" in
  start)
    start_shim
    # shim 通不通先验一下
    curl -sk --max-time 10 "http://127.0.0.1:$SHIM_PORT/" | head -c 300; echo
    start_kibana
    wait_ready
    log "看板入口: http://127.0.0.1:$KB_PORT/app/dashboards#/view/bench-overview"
    log "外部访问: ssh -L $KB_PORT:127.0.0.1:$KB_PORT root@203.0.113.10"
    ;;
  stop)
    for p in "$PID_KB" "$PID_SHIM"; do
      [ -f "$p" ] || continue
      pid=$(cat "$p"); kill "$pid" 2>/dev/null && log "已停 pid=$pid"
      rm -f "$p"
    done
    pkill -f 'shim-7.17-es250.js' 2>/dev/null || true
    ;;
  status)
    for p in "$PID_KB" "$PID_SHIM"; do
      if [ -f "$p" ] && kill -0 "$(cat $p)" 2>/dev/null; then echo "$p: 运行中 (pid $(cat $p))"; else echo "$p: 未运行"; fi
    done
    curl -s --max-time 5 "http://127.0.0.1:$KB_PORT/api/status" | head -c 200; echo
    ;;
  *) die "用法: $0 start|stop|status" ;;
esac
