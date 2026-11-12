#!/usr/bin/env bash
# ============================================================================
# cpu-affinity.sh —— 三节点引擎 CPU 绑核工具（模拟 8 核招标环境）
#   ./cpu-affinity.sh apply <N>  三节点 easysearch+elasticsearch 写 CPUAffinity drop-in
#                                （下次启动生效；JDK 按亲和掩码把可用核数收缩为 N，线程池/GC 按 N 核）
#   ./cpu-affinity.sh status [N] 查看引擎进程当前实际亲和核数（给 N 则校验，不符 rc=1）
#   ./cpu-affinity.sh clear      移除 drop-in（服务下次启动恢复全部 32 vCPU）
#
# 编排侧配套：run-bench*.sh 设 EXPECT_CPU_CORES=N 时每场断言（CPU-GATE-FAIL）。
# ⚠️ drop-in 持久生效：跑完绑核 campaign 后，跑全核矩阵前必须 clear。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

cmd="${1:?用法: $0 <apply N|status [N]|clear>}"
load_topology
[ "${#VM_IPS[@]}" -gt 0 ] || die "拓扑为空，拒绝零操作成功"
declare -A DIR_OF=( [easysearch]="$(engine_root easysearch)" [elasticsearch]="$(engine_root elasticsearch)" [opensearch]="$(engine_root opensearch)" )

case "$cmd" in
  apply)
    n="${2:?用法: apply <N>}"
    cpu_affinity_apply "$n"
    ;;
  clear)
    cpu_affinity_clear
    ;;
  status)
    want="${2:-}" rc=0; seen=0
    [ -z "$want" ] || [[ "$want" =~ ^[1-9][0-9]*$ ]] || die "核数非法: $want"
    for ip in "${VM_IPS[@]}"; do
      for svc in "${ENGINES[@]}"; do
        l=$(cpu_allowed_of "$ip" "$svc" "${DIR_OF[$svc]}") || { echo "$ip $svc: 查询失败"; rc=1; continue; }
        if [ -z "$l" ]; then echo "$ip $svc: 进程未运行"; continue; fi
        seen=$((seen+1))
        n=$(cpu_list_count "$l")
        if [ -n "$want" ] && [ "$n" != "$want" ]; then
          echo "$ip $svc: MISMATCH Cpus_allowed_list=$l（$n 核，期望 $want）"; rc=1
        else
          echo "$ip $svc: Cpus_allowed_list=$l（$n 核）"
        fi
      done
    done
    [ "$seen" -gt 0 ] || { echo "没有可验证的引擎进程"; rc=1; }
    exit $rc
    ;;
  *) die "未知命令: $cmd（可选: apply N | status [N] | clear）";;
esac
