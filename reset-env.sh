#!/usr/bin/env bash
# ============================================================================
# reset-env.sh —— 环境还原（在压测机上运行）
#   ./reset-env.sh <easysearch|elasticsearch|all>        # 停集群 + wipe data/logs（保留安装）
#   ./reset-env.sh ... --no-wipe                         # 只停不清
#   ./reset-env.sh ... --purge                           # 连安装目录一起删除 + 系统参数回滚
#                                                        #   （ibs 模式委托 ops.clean uninstall）
# 说明：不会触碰虚机上原有的 /opt/easysearch/node-4/5/6 目录。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

TARGET="all"; WIPE=1; PURGE=0
for a in "$@"; do
  case "$a" in
    easysearch|elasticsearch|all) TARGET="$a";;
    --no-wipe) WIPE=0;;
    --purge) PURGE=1;;
    *) die "未知参数: $a";;
  esac
done

ENGINES=(easysearch elasticsearch)
[ "$TARGET" = all ] || ENGINES=("$TARGET")

# 拓扑单事实源 = ibs ip.txt：按引擎派生角色节点
declare -a ALL_IPS=()
for eng in "${ENGINES[@]}"; do
  load_topology "$eng"
  log "--- $eng: $NNODES 节点（ip.txt 行 $NODE_RANGE）：${VM_NODES[*]} ---"
  for ip in "${VM_IPS[@]}"; do
    dir=$(eng_dir "$eng")
    if ! vm "$ip" "[ -d $dir ]"; then log "  $eng@$ip: 未安装，跳过"; continue; fi
    stop_engine_on_vm "$ip" "$eng" && log "  $eng@$ip: 已停止"
    u=$(eng_user "$eng")
    if [ "$WIPE" = 1 ] && [ "$PURGE" = 0 ]; then
      if vm "$ip" "rm -rf $dir/data $dir/logs && mkdir -p $dir/data $dir/logs && chown -R $u:$u $dir/data $dir/logs"; then
        log "  $eng@$ip: data/logs 已清空"
      else
        log "  WARN: $eng@$ip 清理失败"
      fi
    fi
    if [ "$PURGE" = 1 ] && [ "$ip" = "${VM_IPS[0]}" ]; then
      # 委托 ibs uninstall（自带 systemd 停止 + PID 清杀 + 删目录 + 删单元，一次覆盖全部角色节点）
      if (cd "$IBS_ROOT" && python3 -m ops.clean uninstall "$eng" --yes >/dev/null 2>&1); then
        log "  $eng: ibs uninstall 完成（全部节点，含 systemd 单元）"
      else
        log "  WARN: $eng ibs uninstall 失败（手动: cd $IBS_ROOT && python3 -m ops.clean uninstall $eng --yes）"
      fi
    fi
    case " ${ALL_IPS[*]} " in *" $ip "*) ;; *) ALL_IPS+=("$ip");; esac
  done
done

# 收尾：监控回滚 + 端口确认（对全部涉猎节点去重执行）
for ip in "${ALL_IPS[@]}"; do
  if [ "$PURGE" = 1 ] && [ "$TARGET" = all ]; then
    vm "$ip" "
      rm -rf $MON_DIR
      [ -f $BENCH_BASE/.sysctl.orig ] && { sysctl -q -w \$(cat $BENCH_BASE/.sysctl.orig); rm -f $BENCH_BASE/.sysctl.orig; }
      rmdir $BENCH_BASE 2>/dev/null
      rm -f /etc/security/limits.d/vm-bench.conf
      echo '  监控目录/系统参数/limits 已还原'
    "
  else
    vm "$ip" "pkill -f 'load-monitor.sh _loo[p]' 2>/dev/null; true"
  fi
  if port_free_on_vm "$ip"; then log "  $ip: 端口 $HTTP_PORT 已释放 ✔"; else log "  WARN: $ip 端口 $HTTP_PORT 仍被占用，请人工检查"; fi
done
log "reset 完成（target=$TARGET wipe=$WIPE purge=$PURGE）"
