#!/usr/bin/env bash
# ============================================================================
# precheck-gate.sh —— 数据可信度门禁（在压测机上运行）
#   ./precheck-gate.sh quiet    # 赛前：三虚机静默检查（load/steal/外来进程/磁盘）
# 集群门禁已统一由 ibs `ops.status gate` 承担（run-bench 内置轮询）。
# 任一检查失败 exit 1，输出 FAILED 原因清单
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

MODE="${1:-quiet}"
[ "$MODE" = quiet ] || die "用法: $0 quiet"

# 拓扑单事实源 = ibs ip.txt（esrally 行是驱动器，不参与门禁）
load_topology

# 把监控脚本分发到虚机（幂等）
push_monitor() {
  local ip
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "mkdir -p $MON_DIR"
    vmPut "$ip" "$LIB_DIR/load-monitor.sh" "$MON_DIR/load-monitor.sh"
    vm "$ip" "chmod +x $MON_DIR/load-monitor.sh"
  done
}

gate_quiet() {
  push_monitor
  local fail=0 ip out load steal free foreign
  for i in "${!VM_IPS[@]}"; do
    ip="${VM_IPS[$i]}"
    out=$(vm "$ip" "ENGINE_MARK='$ENGINE_MARK' $MON_DIR/load-monitor.sh probe 6" 2>/dev/null) || { log "FAIL ${VM_NAMES[$i]}: probe 执行失败"; fail=1; continue; }
    log "  ${VM_NAMES[$i]}: $out"
    load=$(echo "$out"    | grep -o 'load1_avg=[0-9.]*'     | cut -d= -f2)
    steal=$(echo "$out"   | grep -o 'steal_max=[0-9.]*'     | cut -d= -f2)
    foreign=$(echo "$out" | grep -o 'foreign_cpu_max=[0-9.]*' | cut -d= -f2)
    free=$(vm "$ip" "df -BG --output=avail / | tail -1 | tr -d 'G'")
    [ -z "$load" ]    && { log "FAIL ${VM_NAMES[$i]}: probe 无输出"; fail=1; continue; }
    if awk -v a="$load" -v b="$GATE_LOAD1" 'BEGIN{exit !(a>b)}'; then log "FAIL ${VM_NAMES[$i]}: load1=$load > $GATE_LOAD1（虚机不安静）"; fail=1; fi
    if awk -v a="${steal:-0}" -v b="$GATE_STEAL" 'BEGIN{exit !(a>=b)}'; then log "FAIL ${VM_NAMES[$i]}: steal_max=${steal}% >= $GATE_STEAL%（宿主机超卖/邻居噪声）"; fail=1; fi
    if awk -v a="${foreign:-0}" -v b="$GATE_FOREIGN_CPU" 'BEGIN{exit !(a>=b)}'; then log "FAIL ${VM_NAMES[$i]}: 外来进程 CPU ${foreign}% >= $GATE_FOREIGN_CPU%"; fail=1; fi
    if [ "${free:-0}" -lt "$GATE_FREE_GB" ]; then log "FAIL ${VM_NAMES[$i]}: 磁盘仅剩 ${free}G < ${GATE_FREE_GB}G"; fail=1; fi
  done
  [ "$fail" = 0 ] && { log "静默门禁通过 ✔（三虚机安静）"; return 0; } || { log "静默门禁未通过 ✘"; return 1; }
}

gate_quiet
