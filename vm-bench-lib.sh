#!/usr/bin/env bash
# ============================================================================
# vm-bench-lib.sh —— 公共函数（由各脚本 source）
# 运行位置：压测宿主机
# ============================================================================
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 快照中的库仍从原实例读取环境；close/stop 允许未部署环境的本地归档目录。
ENV_FILE="${RACEWAY_ENV_FILE:-$LIB_DIR/vm-bench.env}"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE" || return 1
elif [ "${RACEWAY_ENV_OPTIONAL:-0}" != 1 ]; then
  printf '缺环境配置: %s\n' "$ENV_FILE" >&2; return 1
fi

TS() { date '+%F %T'; }
log()  { echo "[$(TS)] $*"; }
die()  { echo "[$(TS)] ERROR: $*" >&2; exit 1; }

# 路径只允许普通绝对路径，破坏性命令不得接受根目录、点段或 shell 元字符。
ENGINES=(easysearch elasticsearch opensearch)
safe_abs_path() {
  case "$1" in /|''|*[!A-Za-z0-9_./-]*|*/../*|*/..|*/./*|*/.|*//*|*/) return 1;; /*) return 0;; *) return 1;; esac
}
shell_quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
engine_root() { # <engine> → 校验过的统一安装根
  local eng="$1" base="${IBS_BASE:-/infini}"
  case "$eng" in easysearch|elasticsearch|opensearch) ;; *) return 1;; esac
  safe_abs_path "$base" || { log "不安全的 IBS_BASE: $base" >&2; return 1; }
  printf '%s/%s\n' "$base" "$eng"
}

# PID 身份 = boot id + /proc starttime；不能只凭 PID 或命令行子串杀进程。
proc_start() { # <pid>（僵尸不算活跃）
  local pid="$1" line rest
  case "$pid" in ''|*[!0-9]*) return 1;; esac
  [ "$pid" -gt 1 ] || return 1
  line=$(cat "/proc/$pid/stat" 2>/dev/null) || return 1
  rest="${line##*) }"; set -- $rest
  [ "${1:-Z}" != Z ] && [ -n "${20:-}" ] || return 1
  printf '%s\n' "${20}"
}
runner_meta_pid() { # <campaign dir> → 仅返回身份和脚本 argv 均吻合的活 runner
  local dir="$1" pid start boot script extra arg found=0 exe
  [ -f "$dir/RUNNER.meta" ] && [ -f "$dir/RUNNER.pid" ] || return 1
  IFS=$'\t' read -r pid start boot script extra < "$dir/RUNNER.meta" || return 1
  [ -z "${extra:-}" ] && [ "$(cat "$dir/RUNNER.pid")" = "$pid" ] || return 1
  [ "$boot" = "$(cat /proc/sys/kernel/random/boot_id)" ] || return 1
  [ "$(proc_start "$pid")" = "$start" ] || return 1
  exe=$(readlink "/proc/$pid/exe") || return 1
  case "${exe##*/}" in bash|sh|dash) ;; *) return 1;; esac
  while IFS= read -r -d '' arg; do [ "$arg" = "$script" ] && found=1; done < "/proc/$pid/cmdline"
  [ "$found" = 1 ] && [[ "$script" == "$dir"/runtime/*/run-bench.sh ]] || return 1
  printf '%s\n' "$pid"
}
# 远程执行 / 传文件；排程和互斥 FD 不交给 ssh。
vm()    { ssh "${VM_SSH_OPTS[@]}" "root@$1" "$2" </dev/null 9>&-; }
vmPut() { scp "${VM_SSH_OPTS[@]}" "$2" "root@$1:$3" </dev/null 9>&-; }   # vmPut <ip> <local> <remote>

# 对所有虚机执行命令，任一失败即 die
vm_all() {
  local cmd="$1" ip
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "$cmd" || die "在 $ip 上执行失败: $cmd"
  done
}

# JDK 安装（幂等）：把 OpenJDK 装到虚机的 /opt/jdks/jdk-<ver>。
# 只认这一个约定：版本号从目录名取，安装包在 $PKG_DIR/openjdk-<ver>_linux-x64_bin.tar.gz。
# 不猜版本、不下网 —— 装不了就返回非 0，由调用方报错。
provision_jdk() { # <ip>
  local ip="$1" ver pkg
  case "${JDK_DIR:-}" in
    /opt/jdks/jdk-*) ver="${JDK_DIR##*/jdk-}";;
    *) return 1;;                      # 非约定路径 → 不擅自装
  esac
  pkg="$PKG_DIR/openjdk-${ver}_linux-x64_bin.tar.gz"
  [ -s "$pkg" ] || return 1           # 本地没包 → 不擅自下
  log "  $ip: 虚机缺 $JDK_DIR，用本地安装包推送安装（jdk-$ver，约 $(($(stat -c%s "$pkg")/1048576)) MB）"
  vmPut "$ip" "$pkg" "/tmp/openjdk-$ver.tar.gz" >/dev/null || return 1
  vm "$ip" "set -e
    rm -rf /opt/jdks/jdk-$ver.tmp
    mkdir -p /opt/jdks/jdk-$ver.tmp
    tar xzf /tmp/openjdk-$ver.tar.gz -C /opt/jdks/jdk-$ver.tmp --strip-components=1
    rm -rf /opt/jdks/jdk-$ver
    mv /opt/jdks/jdk-$ver.tmp /opt/jdks/jdk-$ver
    rm -f /tmp/openjdk-$ver.tar.gz
    /opt/jdks/jdk-$ver/bin/java -version 2>&1 | head -1" || return 1
  return 0; }

# 引擎名 -> 属性（引擎名 = systemd 服务名：easysearch | elasticsearch）
eng_dir() { engine_root "$1"; }
# 节点命名模板（ibs_sync_ip 改写 ip.txt 时按序套用；来自 env）
node_names_of() { case "$1" in easysearch) echo "${VM_NODES_EZ[@]}";; elasticsearch) echo "${VM_NODES_ES[@]}";; opensearch) echo "${VM_NODES_OS[@]}";; *) return 1;; esac; }
# 服务运行用户（wipe 后 chown 用）
eng_user()   { case "$1" in easysearch) echo "$EZ_RUN_USER";; elasticsearch) echo "$ES_RUN_USER";; opensearch) echo "${OS_RUN_USER:-opensearch}";; esac; }

# ---- 赛道变体（压缩 × source_reuse × 量化）--------------------------------------
# 一个**变体** = 基础赛道 + 压缩档 + source_reuse 开关（量化档是另一维度，见 track_quant）。
#
# 规范 id = <基础赛道id>-codec-<压缩token>[-sr]（与变体目录名一致）：
#   geo-codec-def[-sr] / geo-codec-zstd[-sr] / geo-codec-bc[-sr]
#   hnsw-codec-zstd / ds-j1024-codec-def-sr …（任意基础赛道都适用）
#
# 兼容输入：历史短 id 仍然认（只作别名）：
#   gdef → geo-codec-def
#   gzstd → geo-codec-zstd-sr（是 ZSTD + source_reuse，不是纯 ZSTD）
#   gbc → geo-codec-bc
#
# 压缩 token ↔ index.codec：def=default(LZ4) / zstd=ZSTD / bc=best_compression
# -sr 后缀 = index.source_reuse 开（Easysearch 专有设置）
#
# 压缩与 source_reuse 是索引级设置，对任何基础赛道都成立 → 规则化，不查表。
#
# 引擎兼容（index.codec 取值依据 Easysearch 源码 EngineConfig.INDEX_CODEC_SETTING）：
#   index.codec 合法值 = default / best_compression / lucene_default / ZSTD 系列
#     （ZSTD 系列 Easysearch 专有；ES 的 index.codec 只有 default/best_compression）
#   index.source_reuse = Easysearch 专有（ES 不认这个设置）
#
# → 两侧通用：default/best_compression 且 sr 关
# → 仅 Easysearch：ZSTD（任意 sr）、或任意压缩 + sr 开

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
    *-codec-*)
      # 压缩变体不另建目录：定版在 tracks/，压缩档与 source_reuse 走
      # --track-params（index_codec / source_reuse），由 index.json 的条件块写入。
      local b="${c%%-codec-*}"
      # 只有共享基座目录的量化族（int8/bbq 走 params）指向 hv1024x；
      # 独立目录成员（bbqdisk / os 族，mappings 不同）指向各自目录。
      case "$b" in
        hv1024x|hv1024x-int8|hv1024x-bbq) echo "$ROOT/tracks/hv1024x";;
        *) track_dir "$b";;
      esac;;
    hv1024x-int8|hv1024x-bbq)
      # 向量量化族四臂（hnsw / int8 / bbq / zstd+sr）**一份基座定义**，
      # 量化档走 --track-params（vector_index_type）。
      echo "$ROOT/tracks/hv1024x";;
    geo) echo "$ROOT/tracks/geonames";;
    hnsw) echo "$TRACK_HNSW_S3";;
    hv1024) echo "$ROOT/tracks/hv1024";;
    nested) echo "$ROOT/tracks/nested";;
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
track_quant() { # <id> → index_options.type（空 = 不量化）
  # 向量量化档是**向量索引类型**（index_options.type），不是压缩档：
  # int8_hnsw = 标量量化，bbq_hnsw = 二值量化；ES 8.19.23 与 Easysearch 2.5.1 两侧都能建。
  # 与压缩档是**两个独立维度**，别混：量化走 vector_index_type，压缩走 index_codec。
  case "$(track_base "$1")" in
    hv1024x-os*) echo "";;
    *-int8) echo "int8_hnsw";;
    *-bbq) echo "bbq_hnsw";;
    *) echo "";; esac; }
# hnsw 3 分片变体只在**真要跑 hnsw 族**时才生成：无条件生成时，机器上缺 hnsw 基座
# 定义会让 geo 这类无关赛道也起不来，die 报的却是 hnsw 的事。
tracks_need_hnsw_prep() { # <赛道 id...> → 0=需要生成 hnsw 3 分片变体
  local tk
  for tk in "$@"; do
    case "$tk" in *hnsw*) return 0;; esac
  done
  return 1
}
# 赛道可用性总表：✔=定义在位 / ⚠=不在位。
# run-bench.sh list 与 doctor.sh 共用这一份 —— 两处各写一份映射表 = 迟早漂移。
track_availability_table() {
  local _t _d
  for _t in geo geonames-upsert hnsw hv1024 nested \
            hv1024x hv1024x-int8 hv1024x-bbq hv1024x-codec-zstd-sr \
            hv1024x-bbqdisk hv1024x-os hv1024x-os-bbq hv1024x-os-disk; do
    case "$_t" in
      hnsw) _d="${TRACK_HNSW_BASE:-}";;                    # 3 分片变体起跑时生成，判基座在不在
      *)    _d=$(track_dir "$_t" 2>/dev/null || echo "");;   # 变体与基座同目录（压缩/量化走 params）
    esac
    if [ -n "$_d" ] && [ -f "$_d/track.json" ]; then
      printf '  %s %-24s %s\n' "✔" "$_t" "$_d"
    else
      [ -n "$_d" ] || { case "$_t" in
        hnsw) _d="（vm-bench.env 里没设 TRACK_HNSW_BASE）";;
        *)    _d="（路径解析失败）";;
      esac; }
      printf '  %s %-24s %s\n' "⚠" "$_t" "$_d"
      echo "      ↳ 定义不在位：仓库没同步全（同步得带 tracks/），或这条还没回灌进 git（见 tracks/README.md）"
    fi
  done
  echo "  ⚠ ds-<档名>              $ROOT/tracks/ds-<档名>"
  echo "      ↳ 7 档维度扫描的定义待回灌（语料在机器上）；要跑先按 tracks/README.md「还没切完的部分」回灌"
}
track_index() { # <id> → 索引名（变体沿用基础赛道的索引名）
  local b; b=$(track_base "$1")
  case "$b" in
    geo) echo "geonames";;
    geonames-upsert) echo "geonames-upsert";;
    nested) echo "sonested";;
    *) echo "vectors";;
  esac; }
track_engines() { # <id> → 允许的引擎（空 = 两侧通用）
  local comp sr
  track_is_variant "$1" || { echo ""; return 0; }
  comp=$(track_compression "$1"); sr=$(track_source_reuse "$1")
  if [ "$sr" = "true" ] || [ "$comp" = "ZSTD" ]; then echo "easysearch"; else echo ""; fi
}

track_params() { # <id> → esrally --track-params（空 = 不传）
  # 分片口径：只有"模板类基座"需要传 —— geonames / nested / hv1024 的 index.json
  # 是 Jinja 模板（"index.number_of_shards": {{number_of_shards | default(N)}}），
  # 不传就落到模板默认值（geonames 5、nested 1、hv1024 3），与其余赛道的 3 分片口径
  # 不一致，跨赛道/跨变体的数字就不可比了。
  #
  # 其余赛道（hnsw/ds-*）分片已烘进 index.json，不传。
  # ⚠️ 必须按**基座**判断，不能按 id 名单判断：canonical 变体名与 legacy 别名
  #    走的是同一条规则，只是名字不同。
  local b; b=$(track_base "$1")
  local p=""
  case "$b" in
    geo|nested|hv1024|geonames-upsert) p="number_of_shards:${SHARDS:-3},number_of_replicas:${REPLICAS:-0}";;
  esac
  # 压缩档与 source_reuse：索引级设置，走 --track-params（标量，无逗号，rally 2.12 解析可靠），
  # 由 index.json 的条件块写入 settings（{% if index_codec is defined %} /
  # {% if source_reuse is defined and source_reuse %}）。
  # ⚠️ source_reuse 只在开的时候传：写 false 会被 Jinja 当成非空字符串（真值）→ 反而打开
  #    Easysearch 专有设置，必须不传。
  local comp sr
  comp=$(track_compression "$1"); sr=$(track_source_reuse "$1")
  [ -n "$comp" ] && p="${p:+$p,}index_codec:$comp"
  [ "$sr" = "true" ] && p="${p:+$p,}source_reuse:true"
  # 向量量化档：index_options.type（int8_hnsw / bbq_hnsw）。
  # ⚠️ 它在 **mappings** 里，不在 settings —— 条件块缺失时 rally 不报错，
  # 但该臂会静默跑成 hnsw 基座（"int8 跑过"其实跑的是 fp32）。
  local q; q=$(track_quant "$1")
  [ -n "$q" ] && p="${p:+$p,}vector_index_type:$q"
  echo "$p"; }

# 压缩/存储参数的**静默失效**守卫：定版 index.json 里必须有对应条件块。
# 少一个条件块 → rally 不报错，但压缩档根本没写进 settings → 几个臂跑成同一个，
# 数据看着正常、结论全废（比直接报错难查得多）。所以起跑前就挡住。
track_assert_storage_params() { # <赛道id>
  local id="$1" dir idx comp sr q
  dir=$(track_dir "$id"); idx="$dir/index.json"
  comp=$(track_compression "$id"); sr=$(track_source_reuse "$id"); q=$(track_quant "$id")
  if [ -z "$comp" ] && [ "$sr" != "true" ] && [ -z "$q" ]; then return 0; fi
  [ -f "$idx" ] || die "赛道缺 index.json: $idx（$id）"
  if [ -n "$comp" ] && ! grep -q 'index_codec' "$idx"; then
    die "$id: $idx 没有 index_codec 条件块 —— 压缩档会静默失效（各臂跑成同一个压缩档）"
  fi
  if [ "$sr" = "true" ] && ! grep -q 'source_reuse' "$idx"; then
    die "$id: $idx 没有 source_reuse 条件块 —— source_reuse 会静默失效"
  fi
  if [ -n "$q" ] && ! grep -q 'vector_index_type' "$idx"; then
    die "$id: $idx 没有 vector_index_type 条件块 —— 量化档会静默失效（该臂跑成 hnsw 基座）"
  fi
  return 0; }

# ---- 拓扑（单事实源 = ibs ip.txt）---------------------------------------------
# 集群节点 = ip.txt 中主机名不以 esrally 开头的行（esrally 行是驱动器，不进集群）。
# 产出：VM_IPS/VM_NODES、NNODES、NODE_RANGE（ops.deploy -n 的 1-based 行号区间）、
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
#
# ⚠️ 必须等进程真正退出才返回：紧随其后的 precheck 静默卡点把 bench 标记进程的
# CPU（含退出中的残留 JVM）当"残留引擎"——不等净，每场第一个 precheck 大概率失败，
# 白烧 1~2 次重试（2026-11-08 oshv 系 campaign 实测：23 行判定 14 行 SKIP）。
# 远端归属断言共用同一段：exe 必须位于规范安装根，path.home 参数必须精确相等。
engine_remote_guard() { # <engine>（输出远端前置代码，不执行）
  local dir; dir=$(engine_root "$1") || return 1
  printf 'dir=%s\n' "$(shell_quote "$dir")"
  cat <<'SH'
[ -d "$dir" ] || exit 0
[ ! -L "$dir" ] && [ "$(readlink -f "$dir")" = "$dir" ] || exit 1
root_pids() {
  local p exe
  for entry in /proc/[0-9]*; do
    p=${entry##*/}
    exe=$(readlink -f "$entry/exe" 2>/dev/null) || continue
    [ "${exe##*/}" = java ] || continue
    case "$exe" in "$dir"/*) printf '%s\n' "$p";; esac
  done
}
owned_pid() {
  local p="$1" exe arg home=0
  case "$p" in ''|*[!0-9]*) return 1;; esac
  [ "$p" -gt 1 ] || return 1
  exe=$(readlink -f "/proc/$p/exe" 2>/dev/null) || return 1
  case "$exe" in "$dir"/*) ;; *) return 1;; esac
  while IFS= read -r -d '' arg; do
    case "$arg" in -D*.path.home="$dir") home=1;; esac
  done < "/proc/$p/cmdline"
  [ "$home" = 1 ]
}
pid_start() {
  local line rest
  line=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  rest="${line##*) }"; set -- $rest
  [ "${1:-Z}" != Z ] && [ -n "${20:-}" ] || return 1
  printf '%s' "${20}"
}
signal_owned() {
  local p="$1" sig="$2" start
  start=$(pid_start "$p") || return 0
  owned_pid "$p" || { echo "拒绝操作归属不明 PID $p ($dir)" >&2; return 1; }
  [ "$(pid_start "$p")" = "$start" ] || return 1
  kill "-$sig" "$p" 2>/dev/null || [ ! -e "/proc/$p" ]
}
SH
}
stop_engine_on_vm() { # <ip> <engine>：只有确认全部退出才成功
  local ip="$1" eng="$2" guard
  guard=$(engine_remote_guard "$eng") || return 1
  vm "$ip" "$guard
    for p in \$(root_pids); do owned_pid \"\$p\" || exit 1; done
    main=\$(systemctl show '$eng' -p MainPID --value 2>/dev/null) || exit 1
    case \"\$main\" in 0|'') ;; *[!0-9]*) exit 1;; *) owned_pid \"\$main\" || exit 1;; esac
    systemctl stop '$eng' 2>/dev/null || true
    for i in \$(seq 1 15); do
      pids=\$(root_pids)
      [ -z \"\$pids\" ] && exit 0
      for p in \$pids; do signal_owned \"\$p\" TERM || exit 1; done
      sleep 1
    done
    for p in \$(root_pids); do signal_owned \"\$p\" KILL || exit 1; done
    for i in \$(seq 1 5); do
      [ -z \"\$(root_pids)\" ] && exit 0
      sleep 1
    done
    echo '引擎未停净，拒绝继续' >&2; exit 1"
}
wipe_engine_data_on_vm() { # <ip> <engine>：二次验证停净，只删统一根下 data/logs
  local ip="$1" eng="$2" guard user
  guard=$(engine_remote_guard "$eng") || return 1
  user=$(eng_user "$eng")
  case "$user" in ''|*[!A-Za-z0-9_-]*) return 1;; esac
  vm "$ip" "$guard
    [ -z \"\$(root_pids)\" ] || { echo '仍有引擎进程，拒绝删除' >&2; exit 1; }
    for d in data logs; do [ ! -L \"\$dir/\$d\" ] || exit 1; done
    rm -rf -- \"\$dir/data\" \"\$dir/logs\" &&
    mkdir -p -- \"\$dir/data\" \"\$dir/logs\" &&
    chown -R '$user:$user' \"\$dir/data\" \"\$dir/logs\""
}

# 启动一个引擎
start_engine_on_vm() { # <ip> <engine>
  local ip="$1" eng="$2"
  vm "$ip" "systemctl start $eng"
}

# 断言端口空闲（返回 0=空闲）
port_free_on_vm() { # <ip>；SSH/ss 失败不能视为空闲
  local ip="$1"
  case "${HTTP_PORT:-}" in ''|*[!0-9]*) return 1;; esac
  vm "$ip" "out=\$(ss -H -ltn) || exit 2
    if printf '%s\\n' \"\$out\" | awk '{print \$4}' | grep -qE ':${HTTP_PORT}\$'; then exit 1; fi"
}

# 后缀式段数检查：返回某索引每主分片段数>0 的分片个数（0 = 全部每分片 1 段）
nonseg_shards() { # <index>
  local idx="$1" ip="${VM_IPS[0]}"
  curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 10 \
    "https://$ip:$HTTP_PORT/_cat/segments/$idx?h=prirep,shard" 2>/dev/null \
    | awk '$1=="p"{c[$2]++} END{n=0; for(s in c) if(c[s]>1) n++; print n+0}'
}

# 异步 forcemerge 并轮询到每分片 1 段（编排侧不调用，merge 由赛道 full challenge 负责；
# 函数保留供手工对某个索引做"合并+验证"）。
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

# ---------------------------------------------------------------------------
# 单挑战（full）：写段（建索引/refresh，nested 系写段自带 forcemerge+等合并）
# → forcemerge(1) → 查段，一次 rally 调用跑完（phase=wq）。
# merge 的"完成等待"由赛道自带（force-merge 同步 API / wait-until-merges-finish
# 条件操作，rally 保证完成后才跑后续 op）；编排侧无段数卡点，merge 结果无验证是接受的取舍。
derive_full_challenge() { # <track_dir> —— 从 write-only + query-only 拼出 challenges/full.json（幂等）
  # 派生的三条规则（照做，否则整场 RACE-FAIL）：
  #  (1) 挑战文件优先用 challenges/<name>.json；没有就遍历 challenges/*.json 按挑战名
  #      定位其 schedule 数组（geonames / hnsw2m 把两个挑战写在同一个 default.json 数组里）。
  #  (2) 自检替换占位前先剥 jinja 语句标签，再把引号内占位→"X"、裸占位→"X"；
  #      自检只告警不阻断（真错由 rally 拦）。
  #  (3) rally 要求挑战内任务名唯一：显式 "name" 优先，否则 operation-type；
  #      重名者显式补名（-2/-3…）。
  local dir="$1" out="$1/challenges/full.json"
  [ -s "$out" ] && return 0
  python3 - "$dir" "$out" <<'PY' || return 1
import os, re, json, sys

d, out_path = sys.argv[1], sys.argv[2]
ch_dir = os.path.join(d, "challenges")


def arr_content_at(txt, i):
    """txt[i]=='[' 处的数组**内容**原文（括号配平；字符串/转义感知）。"""
    depth, j, in_str, esc = 0, i, False, False
    while j < len(txt):
        c = txt[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return txt[i + 1:j]
        j += 1
    return None


def split_top_level_ops(content):
    """把 schedule 数组内容切成顶层操作对象的文本列表（保持原样）。"""
    ops, i, n = [], 0, len(content)
    while i < n:
        c = content[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c != "{":
            i += 1
            continue
        start = i
        depth, in_str, esc = 0, False, False
        while i < n:
            ch = content[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
            i += 1
        ops.append(content[start:i])
    return ops


def op_fields(op_text):
    """返回 (显式 name 或 None, operation-type 或 operation 字符串 或 None)。"""
    op_idx = op_text.find('"operation"')
    name_m = re.search(r'"name"\s*:\s*"([^"]+)"', op_text)
    explicit = None
    if name_m and (op_idx < 0 or name_m.start() < op_idx):
        explicit = name_m.group(1)
    otype = None
    tm = re.search(r'"operation-type"\s*:\s*"([^"]+)"', op_text)
    if tm:
        otype = tm.group(1)
    else:
        sm = re.search(r'"operation"\s*:\s*"([^"]+)"', op_text)
        if sm:
            otype = sm.group(1)
    return explicit, otype


def uniquify_ops(ops):
    """rally 要求挑战内任务名唯一：有效名 = 显式 name，否则 operation-type。冲突者显式补名。"""
    seen = {}
    out_ops = []
    for op in ops:
        explicit, otype = op_fields(op)
        eff = explicit or otype
        if eff is None:
            out_ops.append(op)
            continue
        seen[eff] = seen.get(eff, 0) + 1
        if seen[eff] == 1:
            out_ops.append(op)
            continue
        new_name = "%s-%d" % (eff, seen[eff])
        if explicit:
            op = re.sub(r'"name"\s*:\s*"%s"' % re.escape(explicit),
                        '"name": "%s"' % new_name, op, count=1)
        else:
            op = op[:1] + '"name": "%s", ' % new_name + op[1:]
        out_ops.append(op)
    return out_ops


def named_schedule(path, want):
    """取文件里名为 want 的挑战对象的 schedule 数组内容（文本级，保留 jinja 占位）。"""
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r'"name"\s*:\s*"%s"' % re.escape(want), txt)
    if not m:
        return None
    i = txt.find('"schedule"', m.end())
    if i < 0:
        return None
    j = txt.find("[", i)
    if j < 0:
        return None
    return arr_content_at(txt, j)


def find(want):
    p = os.path.join(ch_dir, want + ".json")
    if os.path.isfile(p):
        s = named_schedule(p, want)
        if s:
            return s
    for fn in sorted(os.listdir(ch_dir)):
        if not fn.endswith(".json") or fn in (want + ".json", "full.json"):
            continue
        if ".bak" in fn:
            continue
        s = named_schedule(os.path.join(ch_dir, fn), want)
        if s:
            return s
    return None


w = find("write-only")
q = find("query-only")
if not w or not q:
    sys.stderr.write("  full challenge 派生失败: %s 缺 write-only/query-only\n" % d)
    sys.exit(1)

w = w.strip().rstrip(",")
q = q.strip().rstrip(",")
merge_ops = []
if '"force-merge"' not in w:  # 写段已自带 merge（如 nested-s3 / geonames write-only）就不重复插
    merge_ops = [
        # include-in-reporting=false：合并成本不进 write/query 指标
        # （与 hv1024 族 baked challenge 的 90773a6 同一口径；派生路径漏了这层）
        '{ "operation": { "operation-type": "force-merge", "max-num-segments": 1,'
        ' "request-timeout": 7200, "include-in-reporting": false } }',
        '{ "name": "refresh-after-force-merge", "operation": "refresh" }',
        '{ "name": "wait-until-merges-finish",'
        ' "operation": { "operation-type": "index-stats", "index": "_all",'
        ' "condition": { "path": "_all.total.merges.current", "expected-value": 0 },'
        ' "retry-until-success": true, "include-in-reporting": false } }',
    ]

# Jinja 条件及逗号必须原样保留；仅在等长遮罩上定位操作，再替换重名任务。
# 不能把解析出的对象重新用逗号 join（条件关闭时会留下双逗号）。
sched = ',\n'.join([w] + merge_ops + [q])
masked = re.sub(r'\{%.*?%\}|\{#.*?#\}|\{\{.*?\}\}',
                lambda m: ' ' * len(m.group(0)), sched, flags=re.S)
spans = []
cursor = 0
for op in split_top_level_ops(masked):
    start = masked.find(op, cursor)
    end = start + len(op)
    spans.append((start, end, sched[start:end]))
    cursor = end
ops = uniquify_ops([raw for _, _, raw in spans])
for (start, end, _), op in reversed(list(zip(spans, ops))):
    sched = sched[:start] + op + sched[end:]
out = ('{\n  "name": "full",\n  "description": '
       '"Indexes the corpus, force-merges to one segment and runs the full query set in one race.",\n'
       '  "schedule": [\n' + sched + '\n  ]\n}\n')

# 自检（告警级）：先剥 jinja 语句标签，再替换占位
chk = re.sub(r"\{%.*?%\}", "", out, flags=re.S)
chk = re.sub(r'"\{\{.*?\}\}"', '"X"', chk, flags=re.S)
chk = re.sub(r"\{\{.*?\}\}", '"X"', chk, flags=re.S)
try:
    parsed = json.loads(chk)
    if not parsed.get("schedule"):
        raise ValueError("schedule 为空")
    print("  full challenge 自检通过（%d 段）" % len(parsed["schedule"]))
except Exception as e:
    sys.stderr.write("  ⚠ full challenge 自检未通过（不阻断，交由 rally 校验）: %s\n" % e)

open(out_path, "w", encoding="utf-8").write(out)
print("full challenge 派生 →", out_path)
PY
}

# ============================================================================
# CPU 绑核（模拟 8 核招标环境）
#   三节点 systemd drop-in 写 CPUAffinity，引擎进程及全部线程（含 GC）限定在指定 vCPU；
#   JDK 10+ 按亲和掩码把 availableProcessors 收缩为 N，线程池/GC 自动按 N 核配置。
#   apply 后对服务"下次启动"生效（ibs wipe 重启即生效）；clear 还原。
#   编排侧：run-bench*.sh 设 EXPECT_CPU_CORES=N 时每场断言（CPU-GATE-FAIL）。
# ============================================================================

CPU_DROPIN=zz-cpu-affinity.conf

cpu_allowed_of() { # <ip> <svc> <dir>；归属、连接或多进程歧义必须失败
  local ip="$1" svc="$2" dir="$3" guard
  [ "$dir" = "$(engine_root "$svc")" ] || return 1
  guard=$(engine_remote_guard "$svc") || return 1
  vm "$ip" "$guard
    pids=\$(root_pids); set -- \$pids
    [ \"\$#\" -eq 1 ] || { [ \"\$#\" -eq 0 ] && exit 0; exit 1; }
    owned_pid \"\$1\" || exit 1
    awk '/Cpus_allowed_list/{print \$2}' /proc/\$1/status"
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

cpu_affinity_apply() { # <ncpu> —— 三节点引擎写 CPUAffinity drop-in
  local n="$1" ip cpus
  [[ "$n" =~ ^[1-9][0-9]*$ ]] || die "cpu_affinity_apply: 核数非法: $n"
  [ "${#VM_IPS[@]}" -gt 0 ] || die "绑核拒绝空节点集合"
  cpus=$(seq -s' ' 0 $(( n - 1 )))
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "set -e; [ \$(nproc --all) -ge '$n' ] || exit 1; for svc in easysearch elasticsearch opensearch; do mkdir -p /etc/systemd/system/\$svc.service.d && printf '[Service]\nCPUAffinity=${cpus}\n' > /etc/systemd/system/\$svc.service.d/${CPU_DROPIN}; done; systemctl daemon-reload" \
      || die "在 $ip 写 CPUAffinity drop-in 失败"
  done
  log "CPUAffinity=${cpus} 已写入三节点 drop-in（easysearch+elasticsearch+opensearch；服务下次启动生效，cpu-affinity.sh clear 还原）"
}

cpu_affinity_clear() { # 移除 drop-in（服务下次启动恢复全部核）
  local ip
  [ "${#VM_IPS[@]}" -gt 0 ] || die "清除绑核拒绝空节点集合"
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "set -e; for svc in easysearch elasticsearch opensearch; do rm -f /etc/systemd/system/\$svc.service.d/${CPU_DROPIN}; done; systemctl daemon-reload" \
      || die "在 $ip 移除 CPUAffinity drop-in 失败"
  done
  log "CPUAffinity drop-in 已移除（服务下次启动恢复全部核）"
}

# 实际堆断言：归位重装/工件推送会把 bundle 原版 1g 堆带回来，user-tags 声明的 heap
# 与实际 -Xmx 可能不一致（声明 16g 实际 1g → 全场数据偏低）→ 必须硬断言。
# 必须在引擎冷启动后调用（集群卡点之后）。堆由 ibs 部署时 configure_jvm_heap 统一设定
# （easysearch 走 jvm.options 堆行、ES 走 jvm.options.d/heap.options，均为内存/2=16g）。
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
