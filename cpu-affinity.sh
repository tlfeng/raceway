#!/usr/bin/env bash
# ============================================================================
# cpu-affinity.sh —— 三节点引擎 CPU 绑核工具（模拟 8 核招标环境）
#   ./cpu-affinity.sh apply <N>  三节点 easysearch+elasticsearch 写 systemd
#                                CPUAffinity drop-in（服务下次启动生效；JDK 按亲
#                                和掩码把可用核数收缩为 N，线程池/GC 自动按 N 核）
#   ./cpu-affinity.sh status [N] 查看引擎进程当前实际亲和核数（给 N 则校验，不符 rc=1）
#   ./cpu-affinity.sh clear      移除 drop-in（服务下次启动恢复全部 32 vCPU）
# 编排侧配套：run-bench*.sh 设 EXPECT_CPU_CORES=N 时每场断言（CPU-GATE-FAIL）。
# ⚠️ drop-in 持久生效：跑完绑核 campaign 后，跑全核矩阵前必须 clear。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

cmd="${1:?用法: $0 <apply N|status [N]|clear>}"
declare -A DIR_OF=( [easysearch]="$IBS_BASE/easysearch" [elasticsearch]="$IBS_BASE/elasticsearch" )

case "$cmd" in
  apply)
    n="${2:?用法: apply <N>}"
    cpu_affinity_apply "$n"
    ;;
  clear)
    cpu_affinity_clear
    ;;
  status)
    want="${2:-}" rc=0
    for ip in "${VM_IPS[@]}"; do
      for svc in easysearch elasticsearch; do
        l=$(cpu_allowed_of "$ip" "$svc" "${DIR_OF[$svc]}")
        if [ -z "$l" ]; then echo "$ip $svc: 进程未运行"; continue; fi
        n=$(cpu_list_count "$l")
        if [ -n "$want" ] && [ "$n" != "$want" ]; then
          echo "$ip $svc: MISMATCH Cpus_allowed_list=$l（$n 核，期望 $want）"; rc=1
        else
          echo "$ip $svc: Cpus_allowed_list=$l（$n 核）"
        fi
      done
    done
    exit $rc
    ;;
  *) die "未知命令: $cmd（可选: apply N | status [N] | clear）";;
esac
