#!/usr/bin/env bash
# ============================================================================
# reset-env.sh —— 环境还原（在压测机上运行）
#   ./reset-env.sh <easysearch|elasticsearch|opensearch|all>   # 停集群 + wipe data/logs（保留安装）
#   ./reset-env.sh ... --no-wipe                         # 只停不清
#   ./reset-env.sh ... --purge                           # 连安装目录一起删除 + 系统参数回滚
#
# --purge 在 ibs 模式委托 ops.clean uninstall。
# 说明：不会触碰虚机上原有的 /opt/easysearch/node-4/5/6 目录。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

TARGET="all"; WIPE=1; PURGE=0
for a in "$@"; do
  case "$a" in
    easysearch|elasticsearch|opensearch|all) TARGET="$a";;
    --no-wipe) WIPE=0;;
    --purge) PURGE=1;;
    *) die "未知参数: $a";;
  esac
done

# 必须覆盖全部三引擎：run_field 每场开跑先用它清场，precheck 紧随其后查
# "残留引擎 CPU"。漏一个引擎 = 上一场那个引擎的 JVM 活着进 precheck → 每场
# 第一个 attempt 必失败（2026-11-08 oshv 系 campaign 实测：23 行判定 14 行 SKIP）。
# 未安装的引擎由循环里的 [ -d $dir ] 检查跳过，没有副作用。
[ "$TARGET" = all ] || ENGINES=("$TARGET")

# 第一遍停净全部引擎；任何节点失败均不进入删除阶段。
load_topology
[ "${#VM_IPS[@]}" -gt 0 ] || die "清场拒绝空节点集合"
for eng in "${ENGINES[@]}"; do
  for ip in "${VM_IPS[@]}"; do
    stop_engine_on_vm "$ip" "$eng" || die "$eng@$ip 停服失败，拒绝清理"
  done
done

# 拓扑单事实源 = ibs ip.txt：按引擎派生角色节点
declare -a ALL_IPS=()
for eng in "${ENGINES[@]}"; do
  load_topology "$eng"
  log "--- $eng: $NNODES 节点（ip.txt 行 $NODE_RANGE）：${VM_NODES[*]} ---"
  for ip in "${VM_IPS[@]}"; do
    dir=$(eng_dir "$eng") || die "不安全的引擎安装根"
    installed=$(vm "$ip" "[ ! -d '$dir' ] || printf Y") || die "$eng@$ip 安装目录检查失败"
    if [ "$installed" != Y ]; then log "  $eng@$ip: 未安装，跳过"; continue; fi
    log "  $eng@$ip: 已确认停止"
    if [ "$WIPE" = 1 ] && [ "$PURGE" = 0 ]; then
      wipe_engine_data_on_vm "$ip" "$eng" || die "$eng@$ip 清理失败"
      log "  $eng@$ip: data/logs 已清空"
    fi
    if [ "$PURGE" = 1 ] && [ "$ip" = "${VM_IPS[0]}" ]; then
      # 委托 ibs uninstall（自带 systemd 停止 + PID 清杀 + 删目录 + 删单元，一次覆盖全部角色节点）
      if (cd "$IBS_ROOT" && python3 -m ops.clean uninstall "$eng" --yes >/dev/null 2>&1); then
        log "  $eng: ibs uninstall 完成（全部节点，含 systemd 单元）"
      else
        die "$eng: ibs uninstall 失败"
      fi
    fi
    case " ${ALL_IPS[*]} " in *" $ip "*) ;; *) ALL_IPS+=("$ip");; esac
  done
done

# 收尾：监控回滚 + 端口确认（对全部涉猎节点去重执行）
for ip in "${ALL_IPS[@]}"; do
  if [ "$PURGE" = 1 ] && [ "$TARGET" = all ]; then
    safe_abs_path "$MON_DIR" && safe_abs_path "$BENCH_BASE" || die "不安全的监控目录"
    vm "$ip" "
      set -e
      rm -rf '$MON_DIR'
      [ -f $BENCH_BASE/.sysctl.orig ] && { sysctl -q -w \$(cat $BENCH_BASE/.sysctl.orig); rm -f $BENCH_BASE/.sysctl.orig; }
      rmdir $BENCH_BASE 2>/dev/null
      rm -f /etc/security/limits.d/vm-bench.conf
      echo '  监控目录/系统参数/limits 已还原'
    " || die "$ip 系统参数回滚失败"
  fi
  # 采样归 runner 的 tag 所有；清场不全局 pkill 其他采样器。
  if port_free_on_vm "$ip"; then log "  $ip: 端口 $HTTP_PORT 已释放 ✔"; else die "$ip 端口 $HTTP_PORT 未确认释放（占用或 SSH/ss 失败）"; fi
done
log "reset 完成（target=$TARGET wipe=$WIPE purge=$PURGE）"
