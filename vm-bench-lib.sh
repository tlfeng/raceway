#!/usr/bin/env bash
# ============================================================================
# vm-bench-lib.sh —— 公共函数（由各脚本 source）
# 运行位置：压测宿主机
# ============================================================================
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=vm-bench.env
source "$LIB_DIR/vm-bench.env"

TS() { date '+%F %T'; }
log()  { echo "[$(TS)] $*"; }
die()  { echo "[$(TS)] ERROR: $*" >&2; exit 1; }

# 远程执行 / 传文件
vm()    { ssh "${VM_SSH_OPTS[@]}" "root@$1" "$2"; }
vmPut() { scp "${VM_SSH_OPTS[@]}" "$2" "root@$1:$3"; }   # vmPut <ip> <local> <remote>

# 对所有虚机执行命令，任一失败即 die
vm_all() {
  local cmd="$1" ip
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "$cmd" || die "在 $ip 上执行失败: $cmd"
  done
}

# 引擎名 -> 属性（正式名：easysearch | elasticsearch；systemd 服务名与引擎名相同）
eng_dir() {
  case "$1" in
    easysearch)    echo "$IBS_BASE/easysearch";;
    elasticsearch) echo "$IBS_BASE/elasticsearch";;
    *) return 1;;
  esac
}
# 节点命名模板（ibs_sync_ip 改写 ip.txt 时按序套用；来自 env）
node_names_of() { case "$1" in easysearch) echo "${VM_NODES_EZ[@]}";; elasticsearch) echo "${VM_NODES_ES[@]}";; *) return 1;; esac; }
# 服务运行用户（wipe 后 chown 用）
eng_user()   { case "$1" in easysearch) echo "$EZ_RUN_USER";; elasticsearch) echo "$ES_RUN_USER";; esac; }

# ---- 赛道变体（压缩 × source_reuse）--------------------------------------------
# 一个**变体** = 基础赛道 + 压缩档 + source_reuse 开关。
#
#   规范 id = <基础赛道id>-codec-<压缩token>[-sr]   （与变体目录名一致）
#     geo-codec-def      geo-codec-def-sr
#     geo-codec-zstd     geo-codec-zstd-sr
#     geo-codec-bc       geo-codec-bc-sr
#     hnsw-codec-zstd    ds-j1024-codec-def-sr  …（任意基础赛道都适用）
#
# 历史短 id（只作**兼容输入**，历史排程文件里就是这么写的）：
#   gdef  → geo-codec-def
#   gzstd → geo-codec-zstd-sr   ← 历史名"gzstd"实为 ZSTD + source_reuse，
#                                  不改名是为了让旧 campaign 的场次名语义不漂移
#   gbc  → geo-codec-bc
#
# 压缩 token ↔ index.codec：def=default(LZ4) / zstd=ZSTD / bc=best_compression
# -sr 后缀 = index.source_reuse 开（Easysearch 专有设置）
#
# 为什么用**规则**而不是查表：压缩与 source_reuse 是索引级设置，对**任何**基础
# 赛道都成立 —— 规则化才能推广到全部赛道，新增赛道不用再改表。
#
# 引擎兼容（据 Easysearch 源码 EngineConfig.INDEX_CODEC_SETTING + index.source_reuse
# 的专有性）：
#   index.codec 合法值 = default / best_compression / lucene_default / ZSTD 系列
#     （ZSTD 系列 Easysearch 专有；ES 的 index.codec 只有 default/best_compression）
#   index.source_reuse = Easysearch 专有（ES 不认这个设置）
#   → 两侧通用：default/best_compression 且 sr 关
#   → 仅 Easysearch：ZSTD（任意 sr）、或任意压缩 + sr 开

track_canonical() { # <短 id> → 规范 id
  case "$1" in
    gdef) echo "geo-codec-def";;
    gzstd) echo "geo-codec-zstd-sr";;
    gbc) echo "geo-codec-bc";;
    *) echo "$1";;
  esac; }
track_is_variant() { # <id> → 是变体则 0
  case "$(track_canonical "$1")" in *-codec-*) return 0;; *) return 1;; esac; }
track_base() { # <id> → 基础赛道 id（变体剥掉 -codec-... 后缀）
  local c; c=$(track_canonical "$1")
  case "$c" in *-codec-*) echo "${c%%-codec-*}";; *) echo "$c";; esac; }
track_dir() { # <赛道/变体 id> → 目录
  local c; c=$(track_canonical "$1")
  case "$c" in
    *-codec-*) echo "$ROOT/tracks/$c";;
    geo) echo "$TRACK_GEO";;
    hnsw) echo "$TRACK_HNSW_S3";;
    hv1024) echo "$TRACK_HV1024";;
    nested) echo "$TRACK_NESTED_S3";;
    *) echo "$ROOT/tracks/$1";;
  esac; }
track_compression() { # <id> → index.codec 取值（空 = 非压缩变体）
  # 先剥掉 -sr 后缀再匹配压缩档 —— 否则 "*-codec-bc" 匹配不上
  # "…-codec-bc-sr"（串尾是 -sr），压缩标签会静默变空。
  local t; t=$(track_canonical "$1")
  case "$t" in *-codec-*-sr) t="${t%-sr}";; esac
  case "$t" in
    *-codec-def) echo "default";;
    *-codec-zstd) echo "ZSTD";;
    *-codec-bc) echo "best_compression";;
    *) echo "";; esac; }
track_source_reuse() { # <id> → true / false（空 = 非压缩变体）
  case "$(track_canonical "$1")" in
    *-codec-*-sr) echo "true";;
    *-codec-*) echo "false";;
    *) echo "";; esac; }
track_index() { # <id> → 索引名（变体沿用基础赛道的索引名）
  local b; b=$(track_base "$1"); case "$b" in geo) echo "geonames";; nested) echo "sonested";; *) echo "vectors";; esac; }
track_engines() { # <id> → 允许的引擎（空 = 两侧通用）
  local comp sr
  track_is_variant "$1" || { echo ""; return 0; }
  comp=$(track_compression "$1"); sr=$(track_source_reuse "$1")
  if [ "$sr" = "true" ] || [ "$comp" = "ZSTD" ]; then echo "easysearch"; else echo ""; fi
}

# ---- 拓扑（单事实源 = ibs ip.txt）---------------------------------------------
# 集群节点 = ip.txt 中主机名不以 esrally 开头的行（esrally 行是驱动器，不进集群）。
# 产出：VM_IPS/VM_NODES（角色节点数组）、NNODES、NODE_RANGE（ops.deploy -n 的 1-based 行号区间）、
#       VM_NAMES（env 显式配置的 KVM 客户机名优先且数量须匹配，否则回退集群节点名）。
# 与引擎无关（行集固定）；引擎切换时由 ibs_sync_ip 改写主机名前缀，行集不变。
load_topology() { # [engine_name]（仅日志用途）
  local f="$IBS_ROOT/configs/ip.txt" row=0 host priv declared=0
  [ -s "$f" ] || die "ibs ip.txt 不存在: $f（拓扑单事实源）"
  VM_IPS=(); VM_NODES=()
  NODE_FIRST=""; NODE_LAST=""
  while read -r _pub priv host _ver; do
    row=$((row+1))
    [ -n "$host" ] || continue
    case "$host" in esrally-*) continue;; esac
    VM_IPS+=("$priv"); VM_NODES+=("$host")
    [ -z "$NODE_FIRST" ] && NODE_FIRST=$row
    NODE_LAST=$row
  done < "$f"
  NNODES=${#VM_IPS[@]}
  [ "$NNODES" -ge 1 ] || die "ip.txt 中没有集群角色节点（esrally 行是驱动器）"
  NODE_RANGE="$NODE_FIRST-$NODE_LAST"
  declare -p VM_NAMES >/dev/null 2>&1 && declared=1
  if [ "$declared" = 0 ] || [ "${#VM_NAMES[@]}" -ne "$NNODES" ]; then VM_NAMES=("${VM_NODES[@]}"); fi
}

# 目标主机串（rally --target-hosts）
target_hosts() {
  local h="" ip
  for ip in "${VM_IPS[@]}"; do h="${h}https://${ip}:${HTTP_PORT},"; done
  echo "${h%,}"
}

# ---- 集群操控（在虚机上执行） --------------------------------------------------

# 停一个引擎（systemctl 双停 + forking 逃逸 JVM 兜底击杀：
# ibs 单元 Type=forking，java 会脱离单元 cgroup，systemctl stop 杀不掉——
# 按 exe 路径在安装根下的 java 进程补刀，与 ibs wipe 的 stop-first 同理）
stop_engine_on_vm() { # <ip> <engine>
  local ip="$1" eng="$2" dir; dir=$(eng_dir "$eng") || return 0
  vm "$ip" "systemctl stop $eng 2>/dev/null; sleep 1; systemctl stop $eng 2>/dev/null
    for p in \$(pgrep -x java 2>/dev/null); do
      case \$(readlink /proc/\$p/exe 2>/dev/null) in $dir/*) kill \$p 2>/dev/null;; esac
    done
    true"
}

# 启动一个引擎
start_engine_on_vm() { # <ip> <engine>
  local ip="$1" eng="$2"
  vm "$ip" "systemctl start $eng"
}

# 断言端口空闲（返回 0=空闲）
port_free_on_vm() { # <ip>
  ! vm "$ip" "ss -tln | grep -q ':$HTTP_PORT '" 2>/dev/null
}

# 后缀式段数检查：返回某索引每主分片段数>0 的分片个数（0 = 全部每分片 1 段）
nonseg_shards() { # <index>
  local idx="$1" ip="${VM_IPS[0]}"
  curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 10 \
    "https://$ip:$HTTP_PORT/_cat/segments/$idx?h=prirep,shard" 2>/dev/null \
    | awk '$1=="p"{c[$2]++} END{n=0; for(s in c) if(c[s]>1) n++; print n+0}'
}

# 异步 forcemerge 并轮询到每分片 1 段
force_merge_to_one() { # <index> <max_wait_sec>
  local idx="$1" maxw="${2:-1200}"
  local ip="${VM_IPS[0]}" n deadline=$((SECONDS + maxw))
  log "  forcemerge $idx -> 1 segment/shard"
  curl -sk -u "$ADMIN_USER:$ADMIN_PW" -XPOST --max-time 3600 \
    "https://$ip:$HTTP_PORT/$idx/_forcemerge?max_num_segments=1" >/dev/null 2>&1
  while [ $SECONDS -lt $deadline ]; do
    n=$(nonseg_shards "$idx")
    [ "$n" = "0" ] && { log "  段数卡点通过: $idx 每主分片 1 段"; return 0; }
    sleep 30
  done
  log "  段数卡点失败: $idx 仍有 $n 个分片 >1 段"
  return 1
}

# ============================================================================
# CPU 绑核（模拟 8 核招标环境）
#   原理：三节点 systemd drop-in 写 CPUAffinity，引擎进程及全部线程（含 GC）
#   限定在指定 vCPU；JDK 10+ 按亲和掩码把 availableProcessors 收缩为 N，
#   线程池/GC 自动按 N 核配置 —— 等效一台 N 核机器，不动虚机、不动安装。
#   apply 后对服务"下次启动"生效（ibs wipe 重启即生效）；clear 还原。
#   编排侧：run-bench*.sh 设 EXPECT_CPU_CORES=N 时每场断言（CPU-GATE-FAIL）。
# ============================================================================

CPU_DROPIN=zz-cpu-affinity.conf

cpu_allowed_of() { # <ip> <svc> <dir> → 引擎进程 Cpus_allowed_list（如 "0-7"）；进程不存在输出空
  local ip="$1" svc="$2" dir="$3" pid
  pid=$(vm "$ip" "pgrep -f 'path.home=${dir%h}[h]' | head -1" 2>/dev/null)
  [ -n "$pid" ] || { echo ""; return 0; }
  vm "$ip" "awk '/Cpus_allowed_list/{print \$2}' /proc/$pid/status" 2>/dev/null
}

cpu_list_count() { # "0-7" 或 "0-3,8" → 核数
  local spec="$1" n=0 part a b
  local -a parts
  IFS=',' read -ra parts <<< "$spec"
  for part in "${parts[@]}"; do
    if [[ "$part" == *-* ]]; then a=${part%-*}; b=${part#*-}; n=$((n + b - a + 1)); else n=$((n + 1)); fi
  done
  echo "$n"
}

cpu_affinity_apply() { # <ncpu> —— 三节点 easysearch+elasticsearch 写 CPUAffinity drop-in
  local n="$1" ip cpus
  [ "$n" -ge 1 ] 2>/dev/null || die "cpu_affinity_apply: 核数非法: $n"
  cpus=$(seq -s' ' 0 $(( n - 1 )))
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "for svc in easysearch elasticsearch; do mkdir -p /etc/systemd/system/\$svc.service.d && printf '[Service]\nCPUAffinity=${cpus}\n' > /etc/systemd/system/\$svc.service.d/${CPU_DROPIN}; done && systemctl daemon-reload" \
      || die "在 $ip 写 CPUAffinity drop-in 失败"
  done
  log "CPUAffinity=${cpus} 已写入三节点 drop-in（easysearch+elasticsearch；服务下次启动生效，cpu-affinity.sh clear 还原）"
}

cpu_affinity_clear() { # 移除 drop-in（服务下次启动恢复全部核）
  local ip
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "rm -f /etc/systemd/system/easysearch.service.d/${CPU_DROPIN} /etc/systemd/system/elasticsearch.service.d/${CPU_DROPIN} && systemctl daemon-reload" \
      || die "在 $ip 移除 CPUAffinity drop-in 失败"
  done
  log "CPUAffinity drop-in 已移除（服务下次启动恢复全部核）"
}

# 实际堆断言（2026-10-16 新增）：归位重装/工件推送会把 bundle 原版 1g 堆带回来，
# user-tags 声明的 heap 与实际 -Xmx 可能不一致（10/15 ds3 事故：声明 16g 实际 1g，ez 全部场 -13%）。
# 必须在引擎冷启动后调用（集群卡点之后）。全车硬断言——堆由 ibs 部署时 configure_jvm_heap
# 统一设定（easysearch 走 jvm.options 堆行、ES 走 jvm.options.d/heap.options，均为内存/2=16g）。
heap_assert() { # <car> <expected:16g> [dir] [svc] → 任一节点实际 -Xmx ≠ 声明则 rc=1
  local car="$1" want="${2:-16g}" dir="${3:-$IBS_BASE/easysearch}" svc="${4:-easysearch}"
  local wg num unit gb ip pid xmx bad=0
  wg=$(echo "$want" | tr 'G' 'g'); wg=${wg%g}
  for ip in "${VM_IPS[@]}"; do
    pid=$(vm "$ip" "ps -eo pid,args | grep 'bin/java' | grep -F '$dir' | grep -v grep | awk '{print \$1}' | head -1")
    if [ -z "$pid" ]; then
      log "  HEAP-ASSERT FAIL $ip: 未发现运行中 java 进程（$dir）"; bad=1; continue
    fi
    xmx=$(vm "$ip" "ps -p $pid -o args= | grep -oE -- '-Xmx[0-9]+[mMgG]' | head -1")
    if [ -z "$xmx" ]; then
      log "  HEAP-ASSERT FAIL $ip: 进程 $pid 无显式 -Xmx（期望 $want）"; bad=1; continue
    fi
    num=$(echo "$xmx" | grep -oE '[0-9]+'); unit=$(echo "$xmx" | grep -oE '[mMgG]$')
    case "$unit" in
      m|M) gb=$(awk -v n="$num" 'BEGIN{printf "%.2f", n/1024}');;
      *)   gb=$num;;
    esac
    if awk -v a="$gb" -v b="$wg" 'BEGIN{exit !(a+0 == b+0)}'; then
      log "  HEAP-ASSERT OK $ip: $pid -Xmx=$xmx = 声明 ${want}"
    else
      log "  HEAP-ASSERT FAIL $ip: 进程 $pid 实际 $xmx ≠ 声明 ${want}（归位/推送丢堆配置？）"; bad=1
    fi
  done
  return $bad
}
