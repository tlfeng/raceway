#!/usr/bin/env bash
# ============================================================================
# run-bench.sh —— 性能编排（配置 × 赛道 × 轮次，在压测机上运行；单入口，esrally 风格）
#   ./run-bench.sh <geo|gdef|gzstd|gbc|hnsw|hv1024|nested|codec|both|all|list> [rounds=3] [配置文件]
#   ./run-bench.sh list                          # 列可用赛道/配置模板/近期 campaign
#
# 配置文件（第 3 参数或 CONF= 环境变量）：每行一个配置，轮内按行序右旋配对交错——
#   例（3 种配置 × 3 轮 = 9 场，r1: A,B,C / r2: C,A,B / r3: B,C,A）：
#     ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 ARM_LABEL=ez3015
#     ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/tencentkona-25.0.4 ARM_LABEL=kona25
#     ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 ARM_LABEL=es819
#   允许的键：ENGINE_NAME ENGINE_VERSION ENGINE_BUILD JDK_DIR ARM_LABEL JDK_WANT（见下）；
#   不写配置文件 = 单配置模式，沿用下面的顶层环境旋钮。
#   模板见 scripts/campaign-configs.example.conf；DRY=1 或首参 --dry-run 只打印排程不执行
#   断点续跑：RACES.md 里已 VALID 的场自动跳过（FRESH=1 强制全重跑）
#   收尾可选 NOTIFY_URL=https://webhook… ：POST 一条 JSON 结果摘要
#
# 引擎身份（正式三元组，安装包按 PKG_DIR + 三元组推导文件名）：
#   ENGINE_NAME     easysearch（默认）| elasticsearch
#   ENGINE_VERSION  如 2.5.0（默认）；elasticsearch 如 8.19.23
#   ENGINE_BUILD    可选，easysearch 的 build 号（默认 3015），拼进包名与版本串；
#                   elasticsearch 没有 build 号，设了直接报错
#
# JDK 旋钮（用户自管变体，框架不做臂管理）：
#   JDK_DIR   可选。虚机上已解压好的 JDK 目录；设置后每场前整目录换入 /infini/easysearch/jdk
#             （首次换装自动归档自带 JDK 为 jdk.bundled，之后按 stamp 幂等；JDK_DIR 清空重跑
#             即自动回切自带）。elasticsearch 用自带 JDK，不支持 JDK_DIR。
#   ARM_LABEL 元数据标签（进 user-tags / sidecar / 场次名），默认 = JDK_DIR basename 或
#             engine 名+版本（如 easysearch-2.5.0-3015）；多配置时标签必须互不相同
#   JDK_WANT  可选期望身份 "major|vendor"（如 "25|OpenJDK"），仅作 sidecar 元数据，无硬性卡点；
#             逐节点实测 java -version 由 sidecar 自动落档（results/<fid>-<ph>-sidecar.json）
#
# 每场: reset(只停集群) → 赛前负载检测 → ibs 引擎就位 → JDK 就位(JDK_DIR 换入/回切)
#       → ibs wipe 冷启动 → 集群卡点(含端口属主) → heap_assert → cpu_affinity_assert
#       → write race → 段数卡点 → query race → ENV 判定 → sidecar/监控归档
# 口径：堆由 ibs 部署统一设定（内存/2=16g）；负载卡点按 PRECHECK_MODE（each 每场 / once 起跑前）；
#   场中 load-monitor 分相位采集（w 写入/m 合并/q 查询，5s）仅记录不判成败。codec 是索引设置维度，靠赛道变体承载（gdef/gzstd/gbc）。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

WHAT="${1:?用法: $0 <geo|gdef|gzstd|gbc|hnsw|hv1024|nested|codec|both|all|list> [rounds] [配置文件]}"
[ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
WHAT="${1:?用法: $0 <geo|gdef|gzstd|gbc|hnsw|hv1024|nested|codec|both|all|list> [rounds] [配置文件]}"
ROUNDS="${2:-3}"
DRY=${DRY:-0}

# ---- 顶层环境旋钮（单配置模式；写了配置文件则以配置文件为准） --------------------
ENGINE_NAME=${ENGINE_NAME:-easysearch}
ENGINE_VERSION=${ENGINE_VERSION:-2.5.0}
ENGINE_BUILD=${ENGINE_BUILD:-}
JDK_DIR=${JDK_DIR:-}
JDK_WANT=${JDK_WANT:-}
SHARDS=${SHARDS:-3}          # 性能口径（非拓扑事实）：默认 3 主分片，可按实验覆盖
REPLICAS=${REPLICAS:-0}
# 静默负载卡点口径：each=每场赛前一次；once=仅 campaign 起跑前一次
PRECHECK_MODE=${PRECHECK_MODE:-each}
case "$PRECHECK_MODE" in each|once) ;; *) die "PRECHECK_MODE 必须是 each|once";; esac
PCTAG=$([ "$PRECHECK_MODE" = once ] && echo campaign-once || echo PASS)
FP_DIR=""; LAST_RACE_ID=""; ENV_ID=${ENV_ID:-bench-3node-32c32g}
CLUSTER_WAIT_SEC=${CLUSTER_WAIT_SEC:-300}   # 集群卡点轮询超时（静默卡点阈值内置默认在 precheck.sh）

# ---- 配置文件（第 3 参数或 CONF=）：每行一个配置，轮内右旋配对交错 --------------
CONF_FILE=${3:-${CONF:-}}
[ "${2:-}" = "--dry-run" ] && DRY=1
CONFIGS=()
if [ -n "$CONF_FILE" ]; then
  [ -s "$CONF_FILE" ] || die "配置文件不存在或为空: $CONF_FILE"
  while IFS= read -r _ln; do
    _ln=$(printf '%s' "$_ln" | sed 's/^[[:space:]]*#.*//; s/[[:space:]]*$//')
    [ -n "$_ln" ] || continue
    CONFIGS+=("$_ln")
  done < "$CONF_FILE"
  [ ${#CONFIGS[@]} -ge 1 ] || die "配置文件没有有效配置行: $CONF_FILE"
else
  CONFIGS=("")    # 单配置哨兵：空行 = 沿用顶层环境旋钮
fi

if [ "$WHAT" = list ]; then
  echo "可用赛道: geo gdef gzstd gbc hnsw hv1024 nested codec both all"
  echo "配置模板: $(ls "$LIB_DIR"/campaign-configs.example.conf 2>/dev/null || echo scripts/campaign-configs.example.conf)"
  echo "近期 campaign（RACES.md 的 VALID/总场）:"
  for d in $(ls -dt "$RESULTS_ROOT"/jdk3-* 2>/dev/null | head -8); do
    [ -f "$d/RACES.md" ] || continue
    v=$(grep -c "| VALID |" "$d/RACES.md"); t=$(grep -c "^| " "$d/RACES.md")
    echo "  $(basename "$d")  VALID $v/$((t-2))"
  done
  exit 0
fi

case "$WHAT" in
  geo)  TRACKS=(geo);;
  gdef|gzstd|gbc) TRACKS=("$WHAT");;    # 单独跑某个 codec 变体
  hnsw) TRACKS=(hnsw);;
  hv1024) TRACKS=(hv1024);;
  nested) TRACKS=(nested);;
  codec) TRACKS=(gdef gzstd gbc);;
  both) TRACKS=(geo hnsw);;
  all) TRACKS=(geo hnsw nested);;
  *) die "track 参数必须是 geo|gdef|gzstd|gbc|hnsw|hv1024|nested|codec|both|all";;
esac

# ---- 配置解析与校验（fail-fast 在建 campaign 目录之前） -------------------------
# 引擎映射（ibs 承载部署；路径/服务按 ENGINE_NAME 取）
EZ_DIR="$IBS_BASE/easysearch"
ES_DIR="$IBS_BASE/elasticsearch"

resolve_engine() { # 依据 ENGINE_NAME/VERSION/BUILD/JDK_DIR/ARM_LABEL/JDK_WANT 解析派生值并校验
  ENGINE_NAME=$(printf '%s' "$ENGINE_NAME" | tr 'A-Z' 'a-z')
  case "$ENGINE_NAME" in
    easysearch)
      [ -n "$ENGINE_BUILD" ] || ENGINE_BUILD=3015   # 当前 2.5.0 线默认 build，可覆盖
      ENGINE_PKG="$PKG_DIR/easysearch-${ENGINE_VERSION}-${ENGINE_BUILD}-linux-amd64-bundle.tar.gz"
      FULL_VER="${ENGINE_VERSION}-${ENGINE_BUILD}";;
    elasticsearch)
      [ -z "$ENGINE_BUILD" ] || die "elasticsearch 没有 build 号，勿设 ENGINE_BUILD"
      ENGINE_PKG="$PKG_DIR/elasticsearch-${ENGINE_VERSION}-linux-x86_64.tar.gz"
      FULL_VER="$ENGINE_VERSION";;
    *) die "未知 ENGINE_NAME: $ENGINE_NAME（可选: easysearch | elasticsearch）";;
  esac
  IS_ES=0; [ "$ENGINE_NAME" = elasticsearch ] && IS_ES=1
  [ -s "$ENGINE_PKG" ] || die "缺安装包: $ENGINE_PKG（ENGINE_NAME=$ENGINE_NAME VERSION=$ENGINE_VERSION BUILD=$ENGINE_BUILD）"
  [ "$IS_ES" = 1 ] && [ -n "$JDK_DIR" ] && die "elasticsearch 用自带 JDK，不支持 JDK_DIR"
  # 标签：默认 JDK_DIR basename / 引擎身份；非法字符折叠为 -（进场次名与文件名）
  if [ -z "$ARM_LABEL" ]; then
    if [ -n "$JDK_DIR" ]; then ARM_LABEL=$(basename "$JDK_DIR"); else ARM_LABEL="${ENGINE_NAME}-${FULL_VER}"; fi
  fi
  ARM_LABEL=$(printf '%s' "$ARM_LABEL" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')
  [ -n "$ARM_LABEL" ] || die "ARM_LABEL 为空"
  # sidecar jdk-want 规范化为 "major|vendor"（缺 vendor 补空）
  JDK_WANT_ARG=""
  case "$JDK_WANT" in *\|*) JDK_WANT_ARG="$JDK_WANT";; "") ;; *) JDK_WANT_ARG="$JDK_WANT|";; esac
}

apply_config() { # <line> —— 应用一行配置（空行=单配置模式，沿用顶层旋钮不动）
  local line="$1"
  [ -n "$line" ] || return 0
  ENGINE_NAME=easysearch; ENGINE_VERSION=2.5.0; ENGINE_BUILD=""
  JDK_DIR=""; JDK_WANT=""; ARM_LABEL=""
  local tok k v
  for tok in $line; do
    k=${tok%%=*}; v=${tok#*=}
    [ "$k" != "$tok" ] || die "配置行格式错误（缺 =）: '$tok'"
    case "$k" in
      ENGINE_NAME)    ENGINE_NAME=$v;;
      ENGINE_VERSION) ENGINE_VERSION=$v;;
      ENGINE_BUILD)   ENGINE_BUILD=$v;;
      JDK_DIR)        JDK_DIR=$v;;
      ARM_LABEL)      ARM_LABEL=$v;;
      JDK_WANT)       JDK_WANT=$v;;
      *) die "配置行未知键: $k（允许: ENGINE_NAME ENGINE_VERSION ENGINE_BUILD JDK_DIR ARM_LABEL JDK_WANT）";;
    esac
  done
  resolve_engine
}

# 逐配置预校验（pkg 存在/es 约束/标签查重），并落 CONF_LABELS/CONF_PKGS 供日志与指纹
declare -a CONF_LABELS CONF_PKGS
if [ -n "${CONFIGS[0]}" ]; then
  _seen="|"
  for _ln in "${CONFIGS[@]}"; do
    apply_config "$_ln"
    case "|$_seen|" in *"|$ARM_LABEL|"*) die "ARM_LABEL 重复: $ARM_LABEL（多配置时标签必须互不相同，否则场次名冲突）";; esac
    _seen="$_seen$ARM_LABEL|"
    CONF_LABELS+=("$ARM_LABEL"); CONF_PKGS+=("$ENGINE_PKG")
  done
else
  resolve_engine
  CONF_LABELS=("$ARM_LABEL"); CONF_PKGS=("$ENGINE_PKG")
fi

# 轮内右旋（对配置索引；r1 原序，之后每轮右移一位——配对交错，摊薄时序效应）
order_for_round() { # <round> → 输出索引序列
  # ⚠️ 同行 local 不得引用同行变量（见 ibs_ready_wait 注释）——off 必须单独一行
  local n=${#CONFIGS[@]} i
  local off=$(( ($1 - 1) % n ))
  for i in $(seq 0 $((n-1))); do echo $(( (i - off + n) % n )); done
}

if [ "$DRY" = 1 ] || [ "${WHAT:-}" = "--dry-run" ]; then
  echo "== DRY RUN 排程（不执行任何操作） =="
  echo "tracks: ${TRACKS[*]}  rounds: $ROUNDS  configs: ${#CONFIGS[@]}"
  echo "| # | 配置 | 引擎身份 | 赛道 | 轮 |"
  echo "|---|---|---|---|---|"
  n=0
  for tk in "${TRACKS[@]}"; do
    for r in $(seq 1 "$ROUNDS"); do
      for idx in $(order_for_round "$r"); do
        n=$((n+1))
        apply_config "${CONFIGS[$idx]}"
        printf "| %d | %s | %s %s | %s | r%d |\n" "$n" "$ARM_LABEL" "$ENGINE_NAME" "$FULL_VER" "$tk" "$r"
      done
    done
  done
  echo "共 $n 场。DRY=1/--dry-run 不会创建 campaign、不会触碰集群。"
  exit 0
fi

# campaign 名前缀默认沿用 jdk3（历史惯例）；可用 CAMP_PREFIX 覆盖，
# 让臂名进 campaign 维度——es819 的批次不该叫 jdk3-*（按批次过滤时看板上会认错臂）
CAMP_ID="${CAMP_PREFIX:-jdk3}-$(date +%Y%m%d-%H%M%S)"
CAMP_DIR="$RESULTS_ROOT/$CAMP_ID"
mkdir -p "$CAMP_DIR/results" "$CAMP_DIR/logs"
PROG="$CAMP_DIR/PROGRESS.txt"; RACES="$CAMP_DIR/RACES.md"
PROGSTEP() { echo "[$(date '+%F %T')] $*" | tee -a "$PROG"; }

# ---------- 引擎映射（ibs 承载部署；路径/服务按 ENGINE_NAME 取） -----------------
EZ_DIR="$IBS_BASE/easysearch"
ES_DIR="$IBS_BASE/elasticsearch"

eng_dir_of()   { case "$1" in elasticsearch) echo "$ES_DIR";; easysearch) echo "$EZ_DIR";; *) return 1;; esac; }
svc2_of()      { case "$1" in elasticsearch) echo elasticsearch;; easysearch) echo easysearch;; *) return 1;; esac; }
ip_node_names_of(){ case "$1" in elasticsearch) echo "${VM_NODES_ES[@]}";; *) echo "${VM_NODES_EZ[@]}";; esac; }

# ---------- ibs 生命周期 ------------------------------------------------------
# 依赖 ibs 副本含 af01883（gate version-core）/ 8b1489a（wipe stop-first）修复
ibs_selfcheck() {
  local miss=""
  grep -q "expected_core" "$IBS_ROOT/ops/status.py" || miss="$miss af01883(gate-version)"
  grep -q "STILL_RUNNING" "$IBS_ROOT/ops/clean.py"  || miss="$miss 8b1489a(wipe-stop-first)"
  [ -z "$miss" ] || die "ibs 副本缺修复:$miss —— 先同步仓库到 $IBS_ROOT"
}

# ip.txt 对齐：集群行的第3列按引擎命名模板重写 + 第4列版本（集群卡点断言源；deploy 只回写版本列）
ibs_sync_ip() { # <engine>
  local eng="$1" f="$IBS_ROOT/configs/ip.txt" ver="$FULL_VER" row=0 i=0 host
  local -a names; names=($(ip_node_names_of "$eng"))
  while read -r _pub _priv host _v; do
    row=$((row+1))
    case "$host" in esrally-*) continue;; esac
    i=$((i+1))
    awk -v r="$row" -v name="${names[$((i-1))]:-$host}" -v ver="$ver" 'NR==r{$3=name;$4=ver} {print}' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    VM_NODES[$((i-1))]=${names[$((i-1))]:-$host}
  done < "$f"
  log "  ip.txt 已对齐: $eng 命名模板 / $ver"
}

ibs_deploy_engine() { # <engine>
  local eng="$1" rc tflag vflag
  case "$eng" in
    easysearch)    tflag="-t easysearch";    vflag="-e $FULL_VER";;
    elasticsearch) tflag="-t elasticsearch"; vflag="-s $FULL_VER";;
    *) return 1;;
  esac
  PROGSTEP "  ibs 部署 $eng ($FULL_VER) 开始（约 5-8 分钟，节点 $NODE_RANGE）..."
  ( cd "$IBS_ROOT" && python3 -m ops.deploy $tflag $vflag -n "$NODE_RANGE" --yes ) \
    > "$CAMP_DIR/logs/ibs-deploy-$eng.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    ibs_sync_ip "$eng"
    PROGSTEP "  ibs 部署 $eng 完成"
  else
    PROGSTEP "  ibs 部署 $eng FAILED rc=$rc（见 logs/ibs-deploy-$eng.log）"
  fi
  return $rc
}

ibs_wipe2() { # <engine> —— 冷重启（停服→清 data/logs→重启→ES admin 自愈）
  local eng="$1" rc
  PROGSTEP "  ibs wipe $(svc2_of "$eng")（$NNODES 节点并行）..."
  ( cd "$IBS_ROOT" && python3 -m ops.clean wipe "$(svc2_of "$eng")" --yes ) \
    > "$CAMP_DIR/logs/ibs-wipe-$eng.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then PROGSTEP "  ibs wipe 完成"
  else PROGSTEP "  ibs wipe FAILED rc=$rc（见 logs/ibs-wipe-$eng.log）"; fi
  return $rc
}

ibs_ready_wait() { # <engine> <timeout_sec> <logfile>
  # ⚠️ 同行 local 不得引用同行变量（set -u 下右侧先于赋值展开，报 unbound）
  local eng="$1" tmo="$2" logf="$3"
  local deadline=$((SECONDS + tmo))
  : > "$logf"
  while [ $SECONDS -lt $deadline ]; do
    if ( cd "$IBS_ROOT" && python3 -m ops.status gate "$(svc2_of "$eng")" --json ) >> "$logf" 2>&1; then
      return 0
    fi
    sleep 10
  done
  return 1
}

# 确保引擎已安装且 build 正确。以 ip.txt 第4列完整 build 号为部署状态单一事实来源：
# REST 只报 version core（2.5.0），无法区分 build 号，live 探测对 build 差异必然漏判。
ensure_engine() { # <engine>
  local eng="$1" dir svc want cur installed
  load_topology "$eng"          # 拓扑单事实源：NNODES/NODE_RANGE 随 ip.txt 刷新
  dir=$(eng_dir_of "$eng"); svc=$(svc2_of "$eng")
  want="$FULL_VER"
  cur=$(awk -v ip="${VM_IPS[0]}" '$1==ip{print $4}' "$IBS_ROOT/configs/ip.txt")
  installed=$(vm "${VM_IPS[0]}" "[ -x $dir/bin/$svc ] && echo Y || echo N")
  if [ "$installed" = "Y" ] && [ "$cur" = "$want" ]; then
    return 0
  fi
  if [ "$installed" = "Y" ]; then
    PROGSTEP "  $eng 已装 '$cur' ≠ 期望 '$want'，ibs uninstall 后重部署..."
  else
    PROGSTEP "  $eng 未安装，部署 $want..."
  fi
  # uninstall/集群卡点按 ip.txt 主机名前缀解析角色节点，切换引擎时先对齐到目标，否则报"没有角色节点"
  ibs_sync_ip "$eng"
  ( cd "$IBS_ROOT" && python3 -m ops.clean uninstall "$svc" --yes ) \
    > "$CAMP_DIR/logs/ibs-uninstall-$eng.log" 2>&1
  ibs_deploy_engine "$eng"
}

# ---------- JDK 就位（用户自管：JDK_DIR=虚机上的目录；空=回 bundle 自带） --------
# 首次换装自动把当前 JDK 归档为 jdk.bundled（旧版归档 jdk.bundled-25 复用）；按
# jdk.path.stamp 幂等跳过；java -version 校验失败回退 jdk.old。不设硬性卡点——身份
# 元数据 = ARM_LABEL + sidecar 逐节点实测 java -version（results/<fid>-<ph>-sidecar.json）。
ensure_jdk() {
  local ip dir="$EZ_DIR" arch want
  [ "$IS_ES" = 1 ] && return 0                 # ES 用自带 JDK
  want=${JDK_DIR:-BUNDLED}
  for ip in "${VM_IPS[@]}"; do
    local stamp; stamp=$(vm "$ip" "cat $dir/jdk.path.stamp 2>/dev/null || echo none")
    [ "$stamp" = "$want" ] && continue
    arch=$(vm "$ip" "for d in jdk.bundled jdk.bundled-25; do [ -d $dir/\$d ] && { echo \$d; break; }; done")
    if [ "$want" = BUNDLED ]; then
      if [ -z "$arch" ]; then
        # 无归档 = 从未换装，当前 jdk 即 bundle 自带 → 直接盖章
        vm "$ip" "echo BUNDLED > $dir/jdk.path.stamp && chown easysearch:easysearch $dir/jdk.path.stamp" \
          || { log "  $ip: 写 stamp 失败"; return 1; }
        log "  $ip: JDK 即自带（无归档，首次盖章）"
        continue
      fi
      if ! vm "$ip" "
        set -e
        rm -rf $dir/jdk.old
        if [ -d $dir/jdk ]; then mv $dir/jdk $dir/jdk.old; fi
        cp -a $dir/$arch $dir/jdk
        echo BUNDLED > $dir/jdk.path.stamp
        chown -R easysearch:easysearch $dir/jdk
      "; then
        log "  $ip: 回切自带 JDK 失败"; return 1
      fi
    else
      if ! vm "$ip" "[ -x '$JDK_DIR/bin/java' ]"; then
        log "  $ip: JDK_DIR 不存在或缺 bin/java: $JDK_DIR"; return 1
      fi
      [ -n "$arch" ] && vm "$ip" "[ -d $dir/$arch ]" || \
        { vm "$ip" "cp -a $dir/jdk $dir/jdk.bundled" || { log "  $ip: 归档自带 JDK 失败"; return 1; }; arch=jdk.bundled; }
      if ! vm "$ip" "
        set -e
        if [ ! -d $dir/$arch ]; then cp -a $dir/jdk $dir/$arch; fi
        rm -rf $dir/jdk.old
        if [ -d $dir/jdk ]; then mv $dir/jdk $dir/jdk.old; fi
        cp -a '$JDK_DIR' $dir/jdk
        echo '$JDK_DIR' > $dir/jdk.path.stamp
        chown -R easysearch:easysearch $dir/jdk
      "; then
        log "  $ip: JDK 换装失败，回退 jdk.old"
        vm "$ip" "if [ -d $dir/jdk.old ]; then rm -rf $dir/jdk; mv $dir/jdk.old $dir/jdk; chown -R easysearch:easysearch $dir/jdk; fi"
        return 1
      fi
    fi
    local v; v=$(vm "$ip" "$dir/jdk/bin/java -version 2>&1 | head -1")
    case "$v" in
      *version*) log "  $ip: JDK 就绪（$want）→ $v";;
      *) log "  $ip: java -version 异常: ${v:-无输出}"; return 1;;
    esac
  done
  return 0
}

# CPU 绑核卡点：EXPECT_CPU_CORES=N 时断言引擎进程实际亲和核数（drop-in 由
# scripts/cpu-affinity.sh apply 预先写入，ibs wipe 重启后生效）；未设置时跳过
cpu_affinity_assert() { # <engine>
  [ -n "${EXPECT_CPU_CORES:-}" ] || return 0
  local eng="$1" ip l n bad=0 dir svc jvmn
  dir=$(eng_dir_of "$eng"); svc=$(svc2_of "$eng")
  for ip in "${VM_IPS[@]}"; do
    l=$(cpu_allowed_of "$ip" "$svc" "$dir")
    if [ -z "$l" ]; then
      log "  CPU-GATE FAIL $ip: $svc 引擎进程未找到"; bad=1; continue
    fi
    n=$(cpu_list_count "$l")
    if [ "$n" != "$EXPECT_CPU_CORES" ]; then
      log "  CPU-GATE FAIL $ip: 引擎亲和核数=$n ($l) 期望 $EXPECT_CPU_CORES"; bad=1; continue
    fi
  done
  # JVM 视角确认（非硬性卡点）：_nodes/os 的 available_processors 应等于绑核数
  jvmn=$(curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 10 "https://${VM_IPS[0]}:$HTTP_PORT/_nodes/os" 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin).get('nodes',{});v={n.get('os',{}).get('available_processors') for n in d.values()};print(v.pop() if len(v)==1 else '?')" 2>/dev/null || echo "?")
  if [ "$jvmn" != "$EXPECT_CPU_CORES" ]; then
    log "  CPU-GATE WARN: JVM availableProcessors=$jvmn ≠ $EXPECT_CPU_CORES（掩码已生效但 JVM 未跟随？查 JDK 版本）"
  fi
  [ $bad -eq 0 ] && log "  绑核断言通过（$NNODES 节点引擎亲和 $EXPECT_CPU_CORES 核，JVM 可见核数=$jvmn）"
  return $bad
}

# ---------- track -------------------------------------------------------------
prepare_hnsw_s3() {
  # ⚠️ .offset 断点文件绝不带入（rally 会从旧断点续传，write 计量失真）
  [ -d "$TRACK_HNSW_S3" ] && rm -f "$TRACK_HNSW_S3/documents-2m.json.offset"
  [ -f "$TRACK_HNSW_BASE/track.json" ] || die "hnsw 基准 track 不存在: $TRACK_HNSW_BASE"
  mkdir -p "$TRACK_HNSW_S3"
  (cd "$TRACK_HNSW_BASE" && tar cf - --exclude='documents-2m.json*' .) | tar xf - --strip-components=1 -C "$TRACK_HNSW_S3"
  ln -sf "$TRACK_HNSW_BASE/documents-2m.json" "$TRACK_HNSW_S3/documents-2m.json"
  rm -f "$TRACK_HNSW_S3/documents-2m.json.offset"
  sed -i 's/"number_of_shards": *2/"number_of_shards": 3/' "$TRACK_HNSW_S3/index.json"
  grep -q '"number_of_shards": 3' "$TRACK_HNSW_S3/index.json" || die "hnsw 3 分片改写失败"
  log "hnsw 3 分片变体已生成: $TRACK_HNSW_S3"
}

track_dir()   { case "$1" in geo) echo "$TRACK_GEO";; gdef) echo "$TRACK_GEO_DEF";; gzstd) echo "$TRACK_GEO_ZSTD_SR";; gbc) echo "$TRACK_GEO_BC";; hnsw) echo "$TRACK_HNSW_S3";; hv1024) echo "$TRACK_HV1024";; nested) echo "$TRACK_NESTED_S3";; esac; }
track_index() { case "$1" in geo|gdef|gzstd|gbc) echo "geonames";; hnsw|hv1024) echo "vectors";; nested) echo "sonested";; esac; }
track_tmo_w() { case "$1" in geo|gdef|gzstd|gbc) echo "$TIMEOUT_GEO_W";; hnsw) echo "$TIMEOUT_HNSW_W";; hv1024) echo "$TIMEOUT_HV1024_W";; nested) echo "$TIMEOUT_NESTED_W";; esac; }
track_tmo_q() { case "$1" in geo|gdef|gzstd|gbc) echo "$TIMEOUT_GEO_Q";; hnsw) echo "$TIMEOUT_HNSW_Q";; hv1024) echo "$TIMEOUT_HV1024_Q";; nested) echo "$TIMEOUT_NESTED_Q";; esac; }
# 变体烘入的 settings（进 user-tags，入库时 codec 是一个维度而不是靠 track 名反推）；
# 非 codec 变体返回空 → 不加标签，历史 campaign 的标签口径不变
track_codec() { case "$1" in gdef) echo "default";; gzstd) echo "ZSTD+source_reuse";; gbc) echo "best_compression";; *) echo "";; esac; }

monitor_start() { # <fid> <eng> —— 被动周期采样（5s，只记录，不做卡点、不触发重试）
  local fid="$1" eng="$2" ip dir
  dir=$(eng_dir_of "$eng")
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "mkdir -p $MON_DIR && ENGINE_MARK='$dir' $MON_DIR/load-monitor.sh start $fid" >/dev/null 2>&1 \
      || log "  警告: $ip 监控启动失败（不影响 race）"
  done
}

monitor_stop() { # <fid> —— 收 CSV + summary，落 monitors/（分相位时间序列，供 ingest 与占用分析）
  local fid="$1" ip k mondir="$CAMP_DIR/monitors"
  mkdir -p "$mondir"
  for k in "${!VM_IPS[@]}"; do
    ip="${VM_IPS[$k]}"
    vm "$ip" "$MON_DIR/load-monitor.sh stop $fid" >/dev/null 2>&1
    vm "$ip" "cat $MON_DIR/mon-$fid.csv" > "$mondir/$fid-${VM_NAMES[$k]}.csv" 2>/dev/null
    { echo "node=${VM_NAMES[$k]} ip=$ip"
      vm "$ip" "$MON_DIR/load-monitor.sh summary $MON_DIR/mon-$fid.csv" 2>/dev/null; } \
      > "$mondir/$fid-${VM_NAMES[$k]}-summary.txt" 2>/dev/null
    vm "$ip" "rm -f $MON_DIR/mon-$fid.csv" >/dev/null 2>&1
  done
  PROGSTEP "  监控摘要 $fid: $(grep -h 'steal_avg' "$mondir/$fid"-*-summary.txt 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i ~ /^steal_(avg|max)=/) printf "%s ",$i}' | tr -d '\n')"
}

env_summary_csv() { # campaign 收尾：把各场 summary 拍平成一张 CSV（bench-env/bench-metric 可直接吃）
  local mondir="$CAMP_DIR/monitors"
  [ -d "$mondir" ] || return 0
  python3 - "$mondir" "${VM_NAMES[@]}" <<'PY' || log "  警告: ENV-SUMMARY.csv 生成失败"
import glob, os, re, sys
d = sys.argv[1]
node_re = "^(.*)-(%s)$" % "|".join(re.escape(n) for n in sys.argv[2:])
rows = []
for p in sorted(glob.glob(os.path.join(d, "*-summary.txt"))):
    txt = open(p, encoding="utf-8", errors="replace").read()
    fid_node = os.path.basename(p)[:-len("-summary.txt")]
    # ⚠️ fid 自己就含多个 "-"，必须按尾部的 "-<虚机名>" 切
    m = re.match(node_re, fid_node)
    if m:
        fid, node = m.group(1), m.group(2)
    else:
        fid, _, node = fid_node.partition("-")
    ph = ""                                   # 相位后缀 -w/-m/-q（历史 campaign 无后缀）
    if fid[-2:] in ("-w", "-m", "-q"):
        ph, fid = fid[-1], fid[:-2]
    kv = dict(re.findall(r"(\w+)=(\S+)", txt))
    kv.update(re.findall(r"(\w+)=([0-9.]+%?)", txt))
    g = lambda k: kv.get(k, "")
    rows.append([fid, ph, node, g("ip"), g("samples"), g("steal_avg"), g("steal_max"),
                 g("foreign_avg"), g("foreign_max"),
                 g("swapin"), (re.search(r"ENV-VERDICT: (\S+)", txt).group(1)
                                                    if re.search(r"ENV-VERDICT: (\S+)", txt) else "")])
out = os.path.join(d, "ENV-SUMMARY.csv")
with open(out, "w", encoding="utf-8") as f:
    f.write("fid,phase,node,ip,samples,steal_avg,steal_max,"
            "foreign_avg,foreign_max,swapin,env_verdict\n")
    for r in rows:
        f.write(",".join(str(x).rstrip("%") for x in r) + "\n")
print("ENV-SUMMARY.csv: %d 行 → %s" % (len(rows), out))
PY
}

# ---------- 单场压测 ----------------------------------------------------------
capture_race_json() { # <fid> <w|q> <rally 日志> —— 按日志里的 race id 精确落档 race.json
  # md 报告只是展示行；race.json 是 rally race store 的机读全量：op_metrics 每项含
  # throughput{min/mean/median/max} + latency/service_time 全分位 + error_rate + duration，
  # 另带 user-tags、cluster 分布版本、total_duration、milestones。
  LAST_RACE_ID=""
  local rid src
  rid=$(grep -oE 'Race id is \[[0-9a-fA-F-]+\]' "$3" 2>/dev/null | head -1 | grep -oE '[0-9a-fA-F-]{8,}')
  src="$HOME/.rally/benchmarks/races/$rid/race.json"
  if [ -n "$rid" ] && [ -s "$src" ]; then
    LAST_RACE_ID="$rid"
    cp "$src" "$CAMP_DIR/results/$1-$2-race.json"
    PROGSTEP "  race.json 落档 $1-$2（raceId $rid）"
  else
    PROGSTEP "  race.json 未落档 $1-$2（raceId=${rid:-日志未解析}）"
  fi
}

env_fingerprint_raw() { # campaign 起跑一次 —— 静态环境指纹落 raw/，供每场 sidecar 引用（bench-env 台账源）
  FP_DIR="$CAMP_DIR/logs/fp"; mkdir -p "$FP_DIR"
  { echo "hostname=$(hostname)"; echo "kernel=$(uname -r)"; echo "nproc=$(nproc)"
    echo "cpu_model=$(lscpu | awk -F: '/^Model name/{gsub(/^ +/,"",$2);print $2;exit}')"
    echo "mem_gb=$(free -g | awk '/^Mem:/{print $2}')"; } > "$FP_DIR/host.txt" 2>&1
  local i ip nm vc mb pin
  for i in "${!VM_IPS[@]}"; do
    ip="${VM_IPS[$i]}"; nm="${VM_NAMES[$i]}"
    vm "$ip" "echo hostname=\$(hostname); echo kernel=\$(uname -r); echo nproc=\$(nproc); \
      echo mem_mb=\$(awk '/MemTotal/{print int(\$2/1024)}' /proc/meminfo); \
      echo disk_avail_gb=\$(df -BG --output=avail / | tail -1 | tr -d ' G'); \
      echo disk_path=\$(findmnt -no SOURCE / 2>/dev/null || echo /); \
      echo disk_type=\$(lsblk -no TYPE \$(findmnt -no SOURCE / 2>/dev/null) 2>/dev/null || echo unknown); \
      echo disk_rota=\$(lsblk -no rota \$(findmnt -no SOURCE / 2>/dev/null) 2>/dev/null || echo unknown); \
      echo thp=\$(cat /sys/kernel/mm/transparent_hugepage/enabled); \
      echo max_map_count=\$(sysctl -n vm.max_map_count); \
      echo swap_lines=\$(tail -n +2 /proc/swaps | wc -l); \
      echo cpu_model=\$(lscpu | awk -F: '/^Model name/{gsub(/^ +/,\"\",\$2);print \$2;exit}'); \
      echo name=$nm" > "$FP_DIR/vm-$ip.txt" 2>&1 || log "  警告: $ip 环境指纹抓取失败"
    pin=$(virsh dumpxml "$nm" 2>/dev/null)
    vc=$(echo "$pin" | sed -n "s/.*<vcpu placement='[^']*'>\([0-9]*\)<\/vcpu>.*/\1/p" | head -1)
    mb=$(echo "$pin" | sed -n "s/.*<currentMemory unit='KiB'>\([0-9]*\).*/\1/p" | head -1)
    echo "$nm|${vc:-0}|${mb:-0}|$(echo "$pin" | grep -c '<vcpupin')" >> "$FP_DIR/virsh.txt"
  done
  log "  环境指纹已抓取: $FP_DIR（bench-env 台账源）"
}

race_metric_ok() { # <fid> <w|q> —— 断言 race.json 真有非 null 的吞吐指标
  # rally rc=0 不等于有数据：写赛若整段落在 warmup 里，op_metrics 的 throughput
  # 全为 null，场会被静默判 VALID。
  local f="$CAMP_DIR/results/$1-$2-race.json"
  [ -s "$f" ] || return 0
  python3 - "$f" "$2" <<'PY' || return 1
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
phase = sys.argv[2]
om = (d.get("results") or {}).get("op_metrics") or []
if not om:
    sys.exit(1)
def has_median(m):
    return (m.get("throughput") or {}).get("median") is not None
if phase == "w":
    bulk = [m for m in om if "docs/s" in ((m.get("throughput") or {}).get("unit") or "")] or om
    sys.exit(0 if all(has_median(m) for m in bulk) and any(has_median(m) for m in bulk) else 1)
# 查询赛：个别 task（refresh/sleep 类）本就无吞吐，要求过半有
good = sum(1 for m in om if has_median(m))
sys.exit(0 if good * 2 >= len(om) else 1)
PY
}

sidecar_capture() { # <fid> <w|q> <tk> <rnd> <att> <track_dir> <track_params> —— 每场 race 一份 sidecar
  local fid="$1" ph="$2" tk="$3" rnd="$4" att="$5" tdir="$6" tpar="$7"
  local d="$FP_DIR/$fid-$ph" eng dir ip ch
  [ -n "$FP_DIR" ] && [ -d "$FP_DIR" ] || { log "  警告: 环境指纹未就绪，跳过 sidecar $fid-$ph"; return 0; }
  eng="$ENGINE_NAME"; dir=$(eng_dir_of "$eng")
  [ "$ph" = w ] && ch="write-only" || ch="query-only"
  mkdir -p "$d"
  cp -f "$FP_DIR/host.txt" "$FP_DIR/virsh.txt" "$CAMP_DIR/PACKAGES.txt" "$d/" 2>/dev/null
  mv "$d/PACKAGES.txt" "$d/packages.txt" 2>/dev/null
  for ip in "${VM_IPS[@]}"; do cp -f "$FP_DIR/vm-$ip.txt" "$d/" 2>/dev/null; done
  curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 25 \
    "https://${VM_IPS[0]}:$HTTP_PORT/_nodes/_all/jvm,os,process,settings" > "$d/nodes-info.txt" 2>&1 || true
  curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 25 \
    "https://${VM_IPS[0]}:$HTTP_PORT/_nodes/_all/stats/jvm,os,process,indices" > "$d/nodes-stats.txt" 2>&1 || true
  # 完整 stats：thread_pool(拒绝/排队) / breaker(熔断) / query_cache(命中) /
  # indices.search.query_time(引擎侧累计查询耗时) / fs(磁盘) 只在全量里有。
  # EasySearch 不认 _nodes/stats/<metric> 形式的指定路径（会 400），所以抓不带 metric 名的全量。
  # 缺这一路的后果：压测中"是不是把线程池打爆了""缓存命中多少""引擎侧累计查询耗时"
  # 全部无从回答 —— 饱和与限流在数据上不可区分。
  curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 25 \
    "https://${VM_IPS[0]}:$HTTP_PORT/_nodes/_all/stats" > "$d/nodes-stats-full.txt" 2>&1 || true
  for ip in "${VM_IPS[@]}"; do vm "$ip" "$dir/jdk/bin/java -version 2>&1" > "$d/jdk-$ip.txt" 2>/dev/null; done
  # 实测生效的索引 settings（codec 类结论的可证明性来源：烘进赛道 ≠ 集群里真是这个值）
  local sidx; sidx=$(track_index "$tk")
  [ -n "$sidx" ] && curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 25 \
    "https://${VM_IPS[0]}:$HTTP_PORT/$sidx/_settings?flat_settings=true" > "$d/index-settings.txt" 2>&1 || true
  python3 "$LIB_DIR/bench-sidecar.py" --raw "$d" --out "$CAMP_DIR/results/$fid-$ph-sidecar.json" \
    --campaign "$CAMP_ID" --fid "$fid" --phase "$ph" --race-id "$LAST_RACE_ID" \
    --arm "$ARM_LABEL" --track "$tk" --challenge "$ch" --round "$rnd" --attempt "$att" \
    --track-dir "$tdir" --track-params "$tpar" --heap "$HEAP" --cores "${EXPECT_CPU_CORES:-}" \
    --pkg "$ENGINE_PKG" --jdk-want "$JDK_WANT_ARG" \
    --env-id "$ENV_ID" \
    || log "  警告: sidecar 生成失败 $fid-$ph（不影响 race）"
}

run_field() { # <conf_idx> <track> <round> <seq> [attempt]
  local idx="$1" tk="$2" rnd="$3" seq="$4" att="${5:-1}"
  apply_config "${CONFIGS[$idx]}"     # 换到本场的配置（引擎/JDK/标签）
  local eng="$ENGINE_NAME"
  local fid="${ARM_LABEL}-${tk}-r${rnd}"
  [ "$att" -gt 1 ] && fid="${fid}-try${att}"
  local track; track=$(track_dir "$tk")
  local idx; idx=$(track_index "$tk")
  local wrep="$CAMP_DIR/results/$fid-w.md"
  local qrep="$CAMP_DIR/results/$fid-q.md"
  local wlog="$CAMP_DIR/logs/$fid-w.log" qlog="$CAMP_DIR/logs/$fid-q.log"
  local HOSTS; HOSTS=$(target_hosts)
  local OPTS="use_ssl:true,basic_auth_user:'$ADMIN_USER',basic_auth_password:'$ADMIN_PW',verify_certs:false"
  local tp=""
  case "$tk" in geo|nested|gdef|gzstd|gbc) tp="number_of_shards:$SHARDS,number_of_replicas:$REPLICAS";; esac
  local t0=$(date +%s)

  PROGSTEP "===== [$seq] $fid 开始 ====="

  # 1) 清场（只停集群）+ 赛前负载检测（each 模式逐场；once 模式只在 campaign 起跑前查一次）
  ./reset-env.sh all --no-wipe >/dev/null 2>&1
  local precheck="$PCTAG"
  if [ "$PRECHECK_MODE" = each ]; then
    if ! ./precheck.sh quiet > "$CAMP_DIR/logs/precheck-$fid.log" 2>&1; then
      precheck="FAIL"
      echo "| $fid | SKIP | precheck=FAIL | - |" >> "$RACES"
      PROGSTEP "[$seq] $fid SKIP: 赛前负载检测未通过（logs/precheck-$fid.log）"; return 1
    fi
  fi

  # 2) 引擎就位 + JDK 就位
  if ! ensure_engine "$eng"; then
    echo "| $fid | ENGINE-FAIL | precheck=$PCTAG | $(( $(date +%s) - t0 ))s |" >> "$RACES"
    PROGSTEP "[$seq] $fid ENGINE-FAIL"; return 1
  fi
  if ! ensure_jdk; then
    echo "| $fid | JDK-SWAP-FAIL | precheck=$PCTAG | $(( $(date +%s) - t0 ))s |" >> "$RACES"
    PROGSTEP "[$seq] $fid JDK-SWAP-FAIL"; return 1
  fi

  # 3) 冷启动 + 集群卡点（含端口属主）+ 堆/核断言
  ibs_wipe2 "$eng" || { echo "| $fid | COLD-START-FAIL | precheck=$PCTAG | - |" >> "$RACES"; PROGSTEP "[$seq] $fid COLD-START-FAIL"; return 1; }
  ibs_ready_wait "$eng" "$CLUSTER_WAIT_SEC" "$CAMP_DIR/logs/ibs-ready-$fid.log" || {
    echo "| $fid | CLUSTER-NOT-READY | precheck=$PCTAG | - |" >> "$RACES"
    PROGSTEP "[$seq] $fid CLUSTER-NOT-READY（logs/ibs-ready-$fid.log）"; return 1; }
  heap_assert "$eng" "$HEAP" "$(eng_dir_of "$eng")" "$(svc2_of "$eng")" || { echo "| $fid | HEAP-ASSERT-FAIL | precheck=$PCTAG | - |" >> "$RACES"; PROGSTEP "[$seq] $fid HEAP-ASSERT-FAIL（实际堆≠声明 heap:$HEAP）"; return 1; }
  cpu_affinity_assert "$eng" || { echo "| $fid | CPU-ASSERT-FAIL | precheck=$PCTAG | - |" >> "$RACES"; PROGSTEP "[$seq] $fid CPU-ASSERT-FAIL"; return 1; }
  monitor_start "$fid-w" "$eng"   # 分相位被动采集：w=写入 m=合并 q=查询（summary 即分相位占用画像），不参与判定

  # 4) write-only race
  PROGSTEP "[$seq] $fid write race 开始（timeout $(track_tmo_w "$tk")s）"
  local rc=0
  local ctag=""; [ -n "${EXPECT_CPU_CORES:-}" ] && ctag=",cores:${EXPECT_CPU_CORES}"
  local cdc=""; [ -n "$(track_codec "$tk")" ] && cdc=",codec:$(track_codec "$tk")"
  timeout "$(track_tmo_w "$tk")" "$RALLY" race --pipeline=benchmark-only \
    --target-hosts="$HOSTS" --track-path="$track" --challenge=write-only \
    ${tp:+--track-params="$tp"} --client-options="$OPTS" \
    --user-tags="campaign:$CAMP_ID,engine:$eng,engine_ver:$FULL_VER,arm:$ARM_LABEL,track:$tk,round:$rnd,nnodes:$NNODES,heap:$HEAP,jdk:$ARM_LABEL${ctag}${cdc}" \
    --report-format=markdown --report-file="$wrep" > "$wlog" 2>&1 || rc=$?
  [ $rc -ne 0 ] && PROGSTEP "[$seq] $fid write race FAILED rc=$rc（尾部: $(tail -2 "$wlog" | head -1)）"
  capture_race_json "$fid" w "$wlog"
  sidecar_capture "$fid" w "$tk" "$rnd" "$att" "$track" "$tp"
  monitor_stop "$fid-w"

  # 5) 段数卡点
  local segok=1
  if [ $rc -eq 0 ]; then
    monitor_start "$fid-m" "$eng"
    force_merge_to_one "$idx" 1200 || segok=0
    monitor_stop "$fid-m"
  fi

  # 6) query-only race
  local rcq=0
  if [ $rc -eq 0 ] && [ $segok -eq 1 ]; then
    monitor_start "$fid-q" "$eng"
    PROGSTEP "[$seq] $fid query race 开始"
    timeout "$(track_tmo_q "$tk")" "$RALLY" race --pipeline=benchmark-only \
      --target-hosts="$HOSTS" --track-path="$track" --challenge=query-only \
      ${tp:+--track-params="$tp"} --client-options="$OPTS" \
      --user-tags="campaign:$CAMP_ID,engine:$eng,engine_ver:$FULL_VER,arm:$ARM_LABEL,track:$tk,round:$rnd,nnodes:$NNODES,heap:$HEAP,jdk:$ARM_LABEL${ctag}${cdc}" \
      --report-format=markdown --report-file="$qrep" > "$qlog" 2>&1 || rcq=$?
    [ $rcq -ne 0 ] && PROGSTEP "[$seq] $fid query race FAILED rc=$rcq"
    capture_race_json "$fid" q "$qlog"
    sidecar_capture "$fid" q "$tk" "$rnd" "$att" "$track" "$tp"
    monitor_stop "$fid-q"
  fi

  # 7) 判定（监控只记录环境事实、不参与成败；成败看 race 结果与指标真实性）
  local t1=$(date +%s)
  local status="VALID" metok=1
  race_metric_ok "$fid" w || { metok=0; PROGSTEP "[$seq] $fid 写赛 throughput 全 null（测量窗口被 warmup 吃掉）"; }
  if [ $metok -eq 1 ]; then
    race_metric_ok "$fid" q || { metok=0; PROGSTEP "[$seq] $fid 查询赛 throughput 过半 null"; }
  fi
  [ $metok -eq 0 ] && status="METRIC-NULL-FAIL"
  [ $rc -ne 0 ]    && status="WRITE-FAIL"
  [ $rcq -ne 0 ]   && status="QUERY-FAIL"
  [ $segok -eq 0 ] && status="SEG-ASSERT-FAIL"

  echo "| $fid | $status | precheck=$PCTAG | $(( (t1-t0)/60 ))min |" >> "$RACES"
  PROGSTEP "[$seq] $fid 完成: $status（$(( (t1-t0)/60 )) 分钟）"
  [ "$status" = "VALID" ] || return 1
  return 0
}

run_field_with_retry() { # <conf_idx> <track> <round> <seq>
  local idx="$1" tk="$2" rnd="$3" seq="$4"
  local lbl="${CONF_LABELS[$idx]}"
  local fid="${lbl}-${tk}-r${rnd}"
  # 断点续跑：同 fid 已有 VALID 场则跳过（FRESH=1 强制全重跑）
  if [ "${FRESH:-0}" != 1 ] && grep -q "^| $fid | VALID |" "$RACES" 2>/dev/null; then
    PROGSTEP "[$seq] $fid 跳过（RACES.md 已有 VALID；FRESH=1 可强制重跑）"
    return 0
  fi
  local max_try="${RETRY_MAX:-3}" att=1
  while [ "$att" -le "$max_try" ]; do
    if [ "$att" -gt 1 ]; then
      PROGSTEP "[$seq] $lbl-$tk-r$rnd 第 $att/$max_try 次尝试（前次失败，60s 后自动重试）"
      sleep 60
    fi
    if run_field "$idx" "$tk" "$rnd" "$seq" "$att"; then
      return 0
    fi
    att=$((att+1))
  done
  PROGSTEP "[$seq] $lbl-$tk-r$rnd 已达最大重试次数（$max_try），放弃本场"
  return 1
}

# ---------- 主流程 ------------------------------------------------------------
ibs_selfcheck

apply_config "${CONFIGS[0]}"
load_topology "$ENGINE_NAME"
PROGSTEP "campaign $CAMP_ID 启动: configs=[${CONF_LABELS[*]}] tracks=${TRACKS[*]} rounds=$ROUNDS（轮内右旋配对交错）topology=$NNODES 节点（ip.txt 行 $NODE_RANGE）"
echo "# RACES — $CAMP_ID" > "$RACES"
echo "| 场次 | 状态 | 赛前卡点 | 用时 |" >> "$RACES"
echo "|---|---|---|---|" >> "$RACES"

# once 模式：场间不再检测负载，起跑前这一次是全 campaign 唯一的静默卡点
if [ "$PRECHECK_MODE" = once ]; then
  PROGSTEP "起跑前静默负载卡点（once 模式，本 campaign 唯一一次）"
  ./precheck.sh quiet > "$CAMP_DIR/logs/precheck-campaign.log" 2>&1 \
    || { tail -20 "$CAMP_DIR/logs/precheck-campaign.log"; die "起跑前负载卡点未通过，campaign 终止"; }
fi
PROGSTEP "口径: heap=$HEAP precheck=$PRECHECK_MODE cores_expect=${EXPECT_CPU_CORES:-全部（不绑核）}"

# 材料指纹（结论可回溯；多配置逐行列出，包指纹去重）
{
  echo "campaign: $CAMP_ID"
  echo "configs: ${#CONFIGS[@]}（轮内右旋）"
  for i in "${!CONFIGS[@]}"; do
    echo "  [$i] ${CONF_LABELS[$i]}: ${CONFIGS[$i]:-（顶层环境旋钮）}"
  done
  for p in "${CONF_PKGS[@]}"; do echo "$p"; done | sort -u | while read -r _p; do
    sha256sum "$_p" 2>/dev/null
  done
} > "$CAMP_DIR/PACKAGES.txt"

env_fingerprint_raw    # bench-env 台账源（静态部分，每场 sidecar 引用同一份）

prepare_hnsw_s3
[ -d "$TRACK_HNSW_S3" ] || die "hnsw 3 分片变体准备失败"

case " ${TRACKS[*]} " in *" hv1024 "*)
  ./prepare-hv1024-track.sh || die "hv1024 赛道准备失败"
  [ -d "$TRACK_HV1024" ] || die "hv1024 赛道准备失败"
;; esac

case " ${TRACKS[*]} " in *" nested "*)
  ./prepare-nested-track.sh || die "nested 赛道准备失败"
  [ -d "$TRACK_NESTED_S3" ] || die "nested 赛道准备失败"
;; esac

case " ${TRACKS[*]} " in *gdef*|*gzstd*|*gbc*)
  ./prepare-codec-track.sh || die "codec 赛道变体准备失败"
  for d in "$TRACK_GEO_DEF" "$TRACK_GEO_ZSTD_SR" "$TRACK_GEO_BC"; do
    [ -d "$d" ] || die "codec 赛道变体缺失: $d"
  done
  # 烘进 index.json 的 settings 逐场可回溯：把三个变体的 settings 差异 + VARIANT.txt 落一份台账
  { echo "# codec 变体台账（settings 烘在副本 index.json，共享基座 $TRACK_GEO 未改动）"
    for d in "$TRACK_GEO_DEF" "$TRACK_GEO_ZSTD_SR" "$TRACK_GEO_BC"; do
      echo "== $d"; cat "$d/VARIANT.txt" 2>/dev/null
      grep -A6 'requests.cache' "$d/index.json" | head -12
    done; } > "$CAMP_DIR/CODEC-VARIANTS.txt"
;; esac

total=0; okcnt=0; seqn=0
for tk in "${TRACKS[@]}"; do
  for r in $(seq 1 "$ROUNDS"); do
    for idx in $(order_for_round "$r"); do
      seqn=$((seqn+1))
      if run_field_with_retry "$idx" "$tk" "$r" "$seqn"; then okcnt=$((okcnt+1)); fi
      total=$((total+1))
    done
  done
done

env_summary_csv   # monitors/*-summary.txt → ENV-SUMMARY.csv（bench-env/bench-metric 可直接吃）
PROGSTEP "campaign 结束: $okcnt/$total 场有效。报告: $CAMP_DIR/results/；汇总: $RACES"
if [ -n "${NOTIFY_URL:-}" ]; then
  curl -sk --max-time 10 -X POST -H 'Content-Type: application/json' \
    -d "{\"campaign\":\"$CAMP_ID\",\"valid\":$okcnt,\"total\":$total,\"races\":\"$RACES\"}" "$NOTIFY_URL" \
    >/dev/null 2>&1 && PROGSTEP "通知已发送: $NOTIFY_URL" || PROGSTEP "通知发送失败（不影响结果）"
fi
for ip in "${VM_IPS[@]}"; do vm "$ip" "rm -rf $EZ_DIR/jdk.old" 2>/dev/null; done
./reset-env.sh all >/dev/null 2>&1 || true
PROGSTEP "已停集群并清理数据（安装与 jdk.bundled* 归档保留；下次不带 JDK_DIR 跑会自动回切自带 JDK）"
