#!/usr/bin/env bash
# ============================================================================
# precheck.sh —— 数据可信度卡点（在压测机上运行）
#   ./precheck.sh quiet    # 赛前：三虚机静默检查（load/steal/外来进程/磁盘）
# 集群卡点已统一由 ibs `ops.status gate` 承担（run-bench 内置轮询）。
# 任一检查失败 exit 1，输出 FAILED 原因清单
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

MODE="${1:-quiet}"
[ "$MODE" = quiet ] || die "用法: $0 quiet"

# 拓扑单事实源 = ibs ip.txt（esrally 行是驱动器，不参与卡点）
load_topology

# 静默卡点阈值（vm-bench.env 可覆盖；缺键时用内置默认，旧 env 不用改也能跑）
QUIET_LOAD1=${QUIET_LOAD1:-3}              # 虚机 1 分钟负载
QUIET_STEAL=${QUIET_STEAL:-2}              # cpu steal %（赛后 ENV 判定同口径：verdict 默认值在 load-monitor.sh 内）
QUIET_BENCH_CPU=${QUIET_BENCH_CPU:-1}      # 被测引擎进程 CPU %（>0 即残留引擎或 ENGINE_MARK 错位）
QUIET_FOREIGN_CPU=${QUIET_FOREIGN_CPU:-5}  # 非被测引擎进程 CPU %
QUIET_FREE_GB=${QUIET_FREE_GB:-20}         # 磁盘可用 GB

# 宿主旁路负载：指标库(9237) / 协议适配层(9202) / Kibana(5601)
#
# 这三个服务跑在**压测机**上，而压测机就是这三台虚机的 hypervisor ——
# 它们和被测引擎抢同一批物理 CPU，还会在压测期间做 ES 后台活动
# （refresh / segment merge）、Kibana 定时轮询、看板渲染。
# 压测窗口里跑这些，测出来的数字就带着"旁路负载"的味道：
# 不是引擎慢，是宿主被占了。实测一次全量导出（nice 19、单线程）就落在
# 某场写赛窗口内 —— steal 恰好还是 0.00%，但"steal=0"不等于"环境干净"，
# 可信度的前提是宿主安静，不能靠事后看 steal 恰好没超。
#
# 只查"这几个端口有没有人在听"，不查进程名（避免误伤别人的同端口服务）。
# 逃生阀：HOST_QUIET_PORTS="" 可关掉这条检查（vm-bench.env 里设）。
HOST_QUIET_PORTS=${HOST_QUIET_PORTS:-"9202 9237 5601"}

host_busy_ports() { # 列出宿主上正在监听的旁路端口（空格分隔）
  local p out=""
  for p in $HOST_QUIET_PORTS; do
    [ -n "$p" ] || continue
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"; then
      out="$out $p"
    elif (exec 3<>/dev/tcp/127.0.0.1/"$p") 2>/dev/null; then
      out="$out $p"; exec 3<&- 2>/dev/null
    fi
  done
  printf '%s' "${out# }"
}

# 把监控脚本分发到虚机（幂等）
push_monitor() {
  local ip
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "mkdir -p $MON_DIR"
    vmPut "$ip" "$LIB_DIR/load-monitor.sh" "$MON_DIR/load-monitor.sh"
    vm "$ip" "chmod +x $MON_DIR/load-monitor.sh"
  done
}

quiet_check() {
  push_monitor
  local fail=0 ip out load steal bench free foreign
  for i in "${!VM_IPS[@]}"; do
    ip="${VM_IPS[$i]}"
    out=$(vm "$ip" "ENGINE_MARK='$ENGINE_MARK' $MON_DIR/load-monitor.sh probe 6" 2>/dev/null) || { log "FAIL ${VM_NAMES[$i]}: probe 执行失败"; fail=1; continue; }
    log "  ${VM_NAMES[$i]}: $out"
    load=$(echo "$out"    | grep -o 'load1_avg=[0-9.]*'     | cut -d= -f2)
    steal=$(echo "$out"   | grep -o 'steal_max=[0-9.]*'     | cut -d= -f2)
    bench=$(echo "$out"   | grep -o 'bench_cpu_avg=[0-9.]*' | cut -d= -f2)
    foreign=$(echo "$out" | grep -o 'foreign_cpu_max=[0-9.]*' | cut -d= -f2)
    free=$(vm "$ip" "df -BG --output=avail / | tail -1 | tr -d 'G'")
    [ -z "$load" ]    && { log "FAIL ${VM_NAMES[$i]}: probe 无输出"; fail=1; continue; }
    if awk -v a="$load" -v b="$QUIET_LOAD1" 'BEGIN{exit !(a>b)}'; then log "FAIL ${VM_NAMES[$i]}: load1=$load > $QUIET_LOAD1（虚机不安静）"; fail=1; fi
    if awk -v a="${steal:-0}" -v b="$QUIET_STEAL" 'BEGIN{exit !(a>=b)}'; then log "FAIL ${VM_NAMES[$i]}: steal_max=${steal}% >= $QUIET_STEAL%（宿主机超卖/邻居噪声）"; fail=1; fi
    if awk -v a="${bench:-0}" -v b="$QUIET_BENCH_CPU" 'BEGIN{exit !(a>=b)}'; then log "FAIL ${VM_NAMES[$i]}: bench 进程 CPU ${bench}% >= $QUIET_BENCH_CPU%（残留引擎或 ENGINE_MARK 错位）"; fail=1; fi
    if awk -v a="${foreign:-0}" -v b="$QUIET_FOREIGN_CPU" 'BEGIN{exit !(a>=b)}'; then log "FAIL ${VM_NAMES[$i]}: 外来进程 CPU ${foreign}% >= $QUIET_FOREIGN_CPU%"; fail=1; fi
    if [ "${free:-0}" -lt "$QUIET_FREE_GB" ]; then log "FAIL ${VM_NAMES[$i]}: 磁盘仅剩 ${free}G < ${QUIET_FREE_GB}G"; fail=1; fi
  done
  # 宿主侧：旁路服务检查（压测机 = 虚机的 hypervisor，旁路负载会污染测量）
  local busy; busy=$(host_busy_ports)
  if [ -n "$busy" ]; then
    log "FAIL 压测机: 端口${busy} 有服务在听（指标库 9237 / 适配层 9202 / Kibana 5601）"
    log "      它们与被测引擎抢 hypervisor 的 CPU，压测期间必须停着"
    log "      停法: systemctl stop easysearch-metric-store.service"
    log "            bash /testing/raceway-next/kibana/deploy-kibana.sh stop"
    fail=1
  fi

  [ "$fail" = 0 ] && { log "静默卡点通过 ✔（三虚机安静）"; return 0; } || { log "静默卡点未通过 ✘"; return 1; }
}

quiet_check
