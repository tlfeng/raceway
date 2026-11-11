#!/usr/bin/env bash
# ============================================================================
# run-bench.sh —— 性能编排（配置 × 赛道 × 轮次，在压测机上运行；单入口，esrally 风格）
#   ./run-bench.sh <geo|gdef|gzstd|gbc|<赛道>-codec-<def|zstd|bc>[-sr]|hnsw|hv1024|nested|hv1024x|hv1024x-int8|hv1024x-bbq|hv1024x-codec-zstd-sr|hv1024x-bbqdisk|hv1024x-os|hv1024x-os-bbq|hv1024x-os-disk|upsert|codec|both|all|ds|ds-<档名>|list> [rounds=3] [配置文件]
#   ./run-bench.sh list                          # 列可用赛道/配置模板/近期 campaign
#   ./run-bench.sh --help                        # 用法页（赛道、三种编排、前置条件）
#
# 配置文件（第 3 参数或 CONF= 环境变量）：每行一个配置，轮内按行序右旋配对交错——
#   例（3 种配置 × 3 轮 = 9 场，r1: A,B,C / r2: C,A,B / r3: B,C,A）：
#     ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 CAR_LABEL=ez3015
#     ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015 JDK_DIR=/opt/jdks/tencentkona-25.0.4 CAR_LABEL=kona25
#     ENGINE_NAME=elasticsearch ENGINE_VERSION=8.19.23 CAR_LABEL=es819
#   允许的键：ENGINE_NAME ENGINE_VERSION ENGINE_BUILD JDK_DIR CAR_LABEL JDK_WANT（见下）；
#   不写配置文件 = 单配置模式，沿用下面的顶层环境旋钮。
#   模板见同目录的 campaign-configs.example.conf；DRY=1 或首参 --dry-run 只打印排程不执行
#   断点续跑：RACES.md 里已 VALID 的场自动跳过（FRESH=1 强制全重跑）
#   RECALL=1 ./run-bench.sh <赛道> ...  单跑模式也打 recall 探针（召回塌了的性能数字不作数；
#                    排程模式由排程行的 recall 列决定，不用这个开关）
#   收尾可选 NOTIFY_URL=https://webhook… ：POST 一条 JSON 结果摘要
#
# 引擎身份（正式三元组，安装包按 PKG_DIR + 三元组推导文件名）：
#   ENGINE_NAME     easysearch（默认）| elasticsearch
#   ENGINE_VERSION  如 2.5.0（默认）；elasticsearch 如 8.19.23
#   ENGINE_BUILD    可选，easysearch 的 build 号（默认 3015），拼进包名与版本串；
#                   elasticsearch 没有 build 号，设了直接报错
#
# JDK 旋钮（用户自管变体，框架不做车管理）：
#   JDK_DIR   可选。虚机上已解压好的 JDK 目录；设置后每场前整目录换入 /infini/easysearch/jdk
#             （首次换装自动归档自带 JDK 为 jdk.bundled，之后按 stamp 幂等；JDK_DIR 清空重跑
#             即自动回切自带）。elasticsearch 用自带 JDK，不支持 JDK_DIR。
#   CAR_LABEL 元数据标签（进 user-tags / sidecar / 场次名），默认 = JDK_DIR basename 或
#             engine 名+版本（如 easysearch-2.5.0-3015）；多配置时标签必须互不相同
#   JDK_WANT  可选期望身份 "major|vendor"（如 "25|OpenJDK"），仅作 sidecar 元数据，无硬性卡点；
#             逐节点实测 java -version 由 sidecar 自动落档（results/<fid>-<ph>-sidecar.json）
#
# 每场: reset(只停集群) → 赛前负载检测 → ibs 引擎就位 → JDK 就位(JDK_DIR 换入/回切)
#       → ibs wipe 冷启动 → 集群卡点(含端口属主) → heap_assert → cpu_affinity_assert
#       → full race（写+合并+查单挑战，phase=wq）→ ENV 判定 → sidecar/监控归档
# 口径：堆由 ibs 部署统一设定（内存/2=16g）；负载卡点按 PRECHECK_MODE（each 每场 / once 起跑前）；
#   load-monitor 单挑战全程一份采样（5s）仅记录不判成败。codec 是索引设置维度，靠赛道变体承载（gdef/gzstd/gbc）。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh
# 版本错配守卫：lib 与 runner 必须成对同步。缺 track_params 时分片口径会静默变空
# → 索引落到赛道模板的默认分片数（geonames 模板默认 5），与其余赛道的 3 不一致，
# 而且不会报错 —— 在这里挡住，别等到数字不可比才发现。
declare -F track_params >/dev/null 2>&1 \
  || die "vm-bench-lib.sh 版本过旧（缺 track_params）：请把整个仓库同步到压测机（scp *.sh *.py *.conf vm-bench.env <压测机>:/opt/raceway/）"

# 赛道/模式名单：用法提示、参数校验、文件头注释三处共用这一份（WHAT_ARGS）。
# 收敛成一份后，文案与实际 case 的漂移由 tests/test_codec_variants.py 挡住。
WHAT_ARGS='geo|gdef|gzstd|gbc|<赛道>-codec-<def|zstd|bc>[-sr]|hnsw|hv1024|nested|hv1024x|hv1024x-int8|hv1024x-bbq|hv1024x-codec-zstd-sr|hv1024x-bbqdisk|hv1024x-os|hv1024x-os-bbq|hv1024x-os-disk|upsert|codec|both|all|ds|ds-<档名>|list'

# 用法页：./run-bench.sh --help —— 压测机是纯命令行环境，参数、赛道清单、前置条件、
# 产物在哪全在这一页里，不用去翻源码。
usage_help() {
  cat <<'EOF'
raceway · 压测编排入口（用法页）

用法
  ./run-bench.sh <赛道> [圈数=3] [配置文件]            跑一场 campaign
  ./run-bench.sh --dry-run <赛道> [圈数] [配置文件]     只打印排程，不执行（秒回）
  ./run-bench.sh list                                  列赛道 + 现在能跑哪些 + 近期 campaign
  ./run-bench.sh --help                                本页

三种编排方式
  单配置     ./run-bench.sh geo 3
             配置走环境变量：ENGINE_NAME / ENGINE_VERSION / ENGINE_BUILD /
             JDK_DIR / CAR_LABEL / JDK_WANT
  多配置     ./run-bench.sh geo 3 my3.conf              每行一个配置（同样 6 个键）
  排程文件   SCHEDULE_FILE=… CARS_FILE=… DS_HOURS=12 ./run-bench.sh ds
             逐场指定「哪台车 × 哪条赛道 × 第几圈」，带时间窗保护

赛道（名字 → 赛道定义在哪）
  geo                    geonames 官方基座           tracks/geonames
  hnsw                   96 维 200 万向量            机器本地（vm-bench.env TRACK_HNSW_BASE）
  hv1024                 1024 维 100 万向量          tracks/hv1024
  nested                 nested 文档                 tracks/nested
  hv1024x                1024 维四臂 · hnsw 基座      tracks/hv1024x
  hv1024x-int8           1024 维四臂 · 标量量化       同上（vector_index_type=int8_hnsw）
  hv1024x-bbq            1024 维四臂 · 二值量化       同上（vector_index_type=bbq_hnsw）
  hv1024x-os             1024 维 OpenSearch · lucene hnsw fp32   tracks/hv1024x-os（knn_vector+method）
  hv1024x-os-bbq         1024 维 OpenSearch · Lucene BBQ(32x)    tracks/hv1024x-os-bbq
  hv1024x-os-disk        1024 维 OpenSearch · on_disk(faiss ivf) tracks/hv1024x-os-disk
  hv1024x-codec-zstd-sr  1024 维四臂 · ZSTD+复用源    同上（index_codec=ZSTD + source_reuse）仅 Easysearch
  hv1024x-bbqdisk        1024 维 ES 8.x · BBQ+磁盘化  tracks/hv1024x-bbqdisk（bbq_disk index_options）
  upsert / geonames-upsert  geonames upsert/update 对比  tracks/geonames-upsert（doc_as_upsert 三段流）
  <赛道>-codec-<def|zstd|bc>[-sr]                    压缩变体（走 --track-params，不另存目录）
  gdef / gzstd / gbc      上面几个压缩变体的历史别名
  codec / both / all      批量跑法（geo-codec-* 六变体 / geo+hnsw / geo+hnsw+nested）
  ds / ds-<档名>          维度扫描（定义待回灌，见 tracks/README.md）

跑起来之后
  tail -f campaigns/<campaign>/PROGRESS.txt   实时进度（当前场次 + 阶段）
  cat    campaigns/<campaign>/RACES.md        逐场判定（只有 VALID 能进结论）
  对比 / 入库 / 收尾：docs/GUIDE.md §5~§7

前置条件（少一样都会在起跑前被拦下，并说清缺什么）
  1. cp vm-bench.env.example vm-bench.env 并填写 —— 缺它连 list 都跑不动
  2. ./doctor.sh —— 六组只读体检，✘ 的先修完再起跑
  3. 仓库改动同步到压测机时**要带上 tracks/**（赛道定义在 git 里，漏了就跑不起来）：
       scp -r *.sh *.py *.conf tracks/ kibana/ vm-bench.env <压测机>:/opt/raceway/
EOF
}

case "${1:-}" in
  --help|-h|help) usage_help; exit 0;;
esac

# 打错赛道名时给个最接近的（别让人去翻源码对名单）
# 顺序：先精确命中，再子串命中（长的优先 —— "hv1024x-int8" 不该被猜成 "hv1024"）
suggest_track() { # <错的名字> → 猜一个 id（空 = 猜不出来）
  local w="$1" c
  for c in hv1024x-codec-zstd-sr hv1024x-int8 hv1024x-bbq hv1024x-bbqdisk \
           hv1024x-os-bbq hv1024x-os-disk hv1024x-os hv1024x hv1024 geonames-upsert \
           hnsw nested geo codec both all ds list gdef gzstd gbc; do
    [ "$w" = "$c" ] && { printf '%s' "$c"; return 0; }
  done
  for c in hv1024x-codec-zstd-sr geo-codec-zstd-sr geo-codec-def-sr geo-codec-bc-sr \
           geo-codec-zstd geo-codec-def geo-codec-bc \
           hv1024x-os-bbq hv1024x-os-disk hv1024x-os hv1024x-bbqdisk \
           hv1024x-int8 hv1024x-bbq hv1024x hv1024 geonames-upsert hnsw nested geo codec both all ds; do
    case "$w" in *"$c"*) printf '%s' "$c"; return 0;; esac
  done
  return 0
}

WHAT="${1:?用法: $0 <$WHAT_ARGS> [rounds] [配置文件]（--help 看完整用法）}"
[ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
WHAT="${1:?用法: $0 <$WHAT_ARGS> [rounds] [配置文件]（--help 看完整用法）}"
ROUNDS="${2:-3}"
DRY=${DRY:-0}

# ---- 顶层环境旋钮（单配置模式；写了配置文件则以配置文件为准） --------------------
ENGINE_NAME=${ENGINE_NAME:-easysearch}
ENGINE_VERSION=${ENGINE_VERSION:-2.5.0}
ENGINE_BUILD=${ENGINE_BUILD:-}
JDK_DIR=${JDK_DIR:-}
JDK_WANT=${JDK_WANT:-}
# CAR_LABEL 也算顶层旋钮（单配置模式下 resolve_engine 要判 -z）；
# set -u 下不初始化，`./run-bench.sh geo 3` 一进 resolve_engine 就崩 unbound。
CAR_LABEL=${CAR_LABEL:-}
SHARDS=${SHARDS:-3}          # 性能口径（非拓扑事实）：默认 3 主分片，可按实验覆盖
REPLICAS=${REPLICAS:-0}
# 静默负载卡点口径：each=每场赛前一次；once=仅 campaign 起跑前一次
PRECHECK_MODE=${PRECHECK_MODE:-each}
case "$PRECHECK_MODE" in each|once) ;; *) die "PRECHECK_MODE 必须是 each|once";; esac
PCTAG=$([ "$PRECHECK_MODE" = once ] && echo campaign-once || echo PASS)
FP_DIR=""; LAST_RACE_ID=""; ENV_ID=${ENV_ID:-bench-3node-32c32g}
CLUSTER_WAIT_SEC=${CLUSTER_WAIT_SEC:-300}   # 集群卡点轮询超时（静默卡点阈值内置默认在 precheck.sh）
# 赛后 ENV 判定阈值（主旋钮在 vm-bench.env；缺键回退脚本内置默认）。
# 必须经 ssh 显式传给虚机侧 load-monitor summary —— 不透传的话虚机侧只用内置默认，
# 用户在 env 里改了也不生效。
: "${VERDICT_STEAL_MAX:=2.0}"; : "${VERDICT_FOREIGN_MAX:=5}"

# ds-*（维度扫描）赛道超时：≥1536 维写入给 14400s，其余 10800s；查询统一 5400s
: "${TIMEOUT_DS_W:=10800}"; : "${TIMEOUT_DS_W_HI:=14400}"; : "${TIMEOUT_DS_Q:=5400}"
# 基础赛道超时内置默认（vm-bench.env 有键则覆盖；set -u 下缺键不再是半途 unbound 崩）
: "${TIMEOUT_GEO_W:=14400}";  : "${TIMEOUT_GEO_Q:=5400}"
: "${TIMEOUT_HNSW_W:=10800}"; : "${TIMEOUT_HNSW_Q:=5400}"
: "${TIMEOUT_HV1024_W:=10800}"; : "${TIMEOUT_HV1024_Q:=5400}"
: "${TIMEOUT_NESTED_W:=14400}"; : "${TIMEOUT_NESTED_Q:=5400}"
# 机器布局旋钮同理给内置默认（与 vm-bench.env.example 一致；env 值永远优先）
: "${IBS_BASE:=/infini}"
: "${PKG_DIR:=$ROOT/pkg}"
: "${RESULTS_ROOT:=$ROOT/campaigns}"
: "${MON_DIR:=$ROOT/monitor}"

# ---- 排程模式（SCHEDULE_FILE + CARS_FILE）--------------------------------------
# 用途：维度扫描这类"高维优先 + 窗底守卫"的固定排程，逐行指定 (车, 赛道, 轮次, recall, est分钟)。
#   SCHEDULE_FILE 每行: <车名> <赛道> <轮次> <recall|norecall> <est分钟>
#   CARS_FILE     每行: <车名> <配置行...>   （配置行语法同 CONF_FILE）
#   DS_HOURS      >0 时启用窗底守卫：放不下下一场就 DEADLINE-SKIP 并继续后面更便宜的场
#   CAMP_ID       显式指定可复用同一 campaign 目录 → STATE.txt 断点续跑
SCHEDULE_FILE=${SCHEDULE_FILE:-}
CARS_FILE=${CARS_FILE:-${ARMS_FILE:-}}   # 旧名 ARMS_FILE 仍认（兼容旧排程命令）
DS_HOURS=${DS_HOURS:-0}
CUR_RECALL=${RECALL:-0}     # 单跑模式是否打 recall 探针（RECALL=1）；排程模式由排程行的 recall 列决定

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
  echo "可用赛道: geo gdef gzstd gbc hnsw hv1024 nested hv1024x hv1024x-int8 hv1024x-bbq hv1024x-codec-zstd-sr hv1024x-bbqdisk hv1024x-os hv1024x-os-bbq hv1024x-os-disk geonames-upsert codec both all ds"
  echo "  单跑某个压缩变体：<赛道>-codec-<def|zstd|bc>[-sr]（如 geo-codec-zstd-sr、geo-codec-bc）"
  echo
  echo "赛道 → 定义目录 → 现在能不能跑："
  track_availability_table
  echo
  echo "配置模板: $(ls "$LIB_DIR"/campaign-configs.example.conf 2>/dev/null || echo campaign-configs.example.conf)"
  echo "近期 campaign（RACES.md 的 VALID/总场）:"
  # 不限 jdk3-* 前缀：车族已分化（jdk3/vm3/esrm/ds3/ds2/es819…），只列 jdk3-*
  # 会把 es819 车批次、维度扫描批次整批漏掉。
  for d in $(ls -dt "$RESULTS_ROOT"/*/ 2>/dev/null | head -8); do
    [ -f "$d/RACES.md" ] || continue
    v=$(grep -c "| VALID |" "$d/RACES.md"); t=$(grep -c "^| " "$d/RACES.md")
    echo "  $(basename "$d")  VALID $v/$((t-2))"
  done
  echo
  echo "完整用法：./run-bench.sh --help"
  exit 0
fi

case "$WHAT" in
  geo)  TRACKS=(geo);;
  *-codec-*) TRACKS=("$WHAT");;     # 单独跑某个压缩变体（任意基础赛道；压缩档走 --track-params）
  gdef|gzstd|gbc) TRACKS=("$WHAT");; # 仍认的短名别名（= geo-codec-*）
  hnsw) TRACKS=(hnsw);;
  hv1024) TRACKS=(hv1024);;
  hv1024x|hv1024x-int8|hv1024x-bbq|hv1024x-bbqdisk|hv1024x-os|hv1024x-os-bbq|hv1024x-os-disk) TRACKS=("$WHAT");;
           # 量化族：四臂 = hnsw 基座 / int8 / bbq / 压缩臂（一份基座 + params）。
           # bbqdisk 与 os 族是**独立目录定义**（mappings 不同，差异表达不了成 params）。
           # ⚠ 这里用**白名单**而不是 hv1024x* 前缀匹配：前缀匹配会把拼错的
           # "hv1024x-int88" 当成合法赛道，一路走到 rally 才炸（报错跟"名字打错"毫无关系）。
           # 压缩臂（hv1024x-codec-*）由上面的 *-codec-* 分支接住。
  upsert|geonames-upsert) TRACKS=(geonames-upsert);;
           # geonames upsert/update 对比赛道（raceway 定制，见 /testing/upsert-bench/）
  nested) TRACKS=(nested);;
  codec) TRACKS=(geo-codec-def geo-codec-def-sr geo-codec-zstd geo-codec-zstd-sr geo-codec-bc geo-codec-bc-sr);;
           # geonames 一族的压缩×source_reuse 全套 6 变体
           # 不兼容的（车引擎 × 变体）在起跑时留痕跳过
  both) TRACKS=(geo hnsw);;
  all) TRACKS=(geo hnsw nested);;
  ds) TRACKS=(ds-g1920 ds-j2048 ds-j1024 ds-g960 ds-j768 ds-j512 ds-j1536);;
  ds-*) TRACKS=("$WHAT");;
  *)
    _sug=$(suggest_track "$WHAT")
    if [ -n "$_sug" ]; then
      die "track 参数必须是 $WHAT_ARGS
  你可能想跑的是：$_sug
  （--help 看完整用法，list 看现在能跑哪些）"
    else
      die "track 参数必须是 $WHAT_ARGS
  （--help 看完整用法，list 看现在能跑哪些）"
    fi;;
esac

# ---- 配置解析与校验（fail-fast 在建 campaign 目录之前） -------------------------
# 引擎映射（ibs 承载部署；路径/服务按 ENGINE_NAME 取）
EZ_DIR="$IBS_BASE/easysearch"
ES_DIR="$IBS_BASE/elasticsearch"

resolve_engine() { # 依据 ENGINE_NAME/VERSION/BUILD/JDK_DIR/CAR_LABEL/JDK_WANT 解析派生值并校验
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
    opensearch)  # os-20261108: 包名 linux-x64（ibs find_local_pkg 按 <engine>-<ver>-*x64* 匹配本地包）
      [ -z "$ENGINE_BUILD" ] || die "opensearch 没有 build 号，勿设 ENGINE_BUILD"
      ENGINE_PKG="$PKG_DIR/opensearch-${ENGINE_VERSION}-linux-x64.tar.gz"
      FULL_VER="$ENGINE_VERSION";;
    *) die "未知 ENGINE_NAME: $ENGINE_NAME（可选: easysearch | elasticsearch | opensearch）";;
  esac
  IS_ES=0; [ "$ENGINE_NAME" = elasticsearch ] && IS_ES=1
  [ -s "$ENGINE_PKG" ] || die "缺安装包: $ENGINE_PKG（ENGINE_NAME=$ENGINE_NAME VERSION=$ENGINE_VERSION BUILD=$ENGINE_BUILD）"
  [ "$IS_ES" = 1 ] && [ -n "$JDK_DIR" ] && die "elasticsearch 用自带 JDK，不支持 JDK_DIR"
  [ "$ENGINE_NAME" = opensearch ] && [ -n "$JDK_DIR" ] && die "opensearch 用自带 JDK，不支持 JDK_DIR"
  # 标签：默认 JDK_DIR basename / 引擎身份；非法字符折叠为 -（进场次名与文件名）
  if [ -z "$CAR_LABEL" ]; then
    if [ -n "$JDK_DIR" ]; then CAR_LABEL=$(basename "$JDK_DIR"); else CAR_LABEL="${ENGINE_NAME}-${FULL_VER}"; fi
  fi
  CAR_LABEL=$(printf '%s' "$CAR_LABEL" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')
  [ -n "$CAR_LABEL" ] || die "CAR_LABEL 为空"
  # sidecar jdk-want 规范化为 "major|vendor"（缺 vendor 补空）
  JDK_WANT_ARG=""
  case "$JDK_WANT" in *\|*) JDK_WANT_ARG="$JDK_WANT";; "") ;; *) JDK_WANT_ARG="$JDK_WANT|";; esac
}

apply_config() { # <line> —— 应用一行配置（空行=单配置模式，沿用顶层旋钮不动）
  local line="$1"
  [ -n "$line" ] || return 0
  ENGINE_NAME=easysearch; ENGINE_VERSION=2.5.0; ENGINE_BUILD=""
  JDK_DIR=""; JDK_WANT=""; CAR_LABEL=""
  local tok k v
  for tok in $line; do
    k=${tok%%=*}; v=${tok#*=}
    [ "$k" != "$tok" ] || die "配置行格式错误（缺 =）: '$tok'"
    case "$k" in
      ENGINE_NAME)    ENGINE_NAME=$v;;
      ENGINE_VERSION) ENGINE_VERSION=$v;;
      ENGINE_BUILD)   ENGINE_BUILD=$v;;
      JDK_DIR)        JDK_DIR=$v;;
      CAR_LABEL)      CAR_LABEL=$v;;
      ARM_LABEL)      CAR_LABEL=$v;;    # 旧名 ARM_LABEL 仍然认
      JDK_WANT)       JDK_WANT=$v;;
      *) die "配置行未知键: $k（允许: ENGINE_NAME ENGINE_VERSION ENGINE_BUILD JDK_DIR CAR_LABEL JDK_WANT）";;
    esac
  done
  resolve_engine
}

# 逐配置预校验（pkg 存在/es 约束/标签查重），并落 CONF_LABELS/CONF_PKGS 供日志与指纹
declare -a CONF_LABELS CONF_PKGS
if [ -n "${SCHEDULE_FILE:-}" ]; then
  # 排程模式：配置在排程推进时逐条注册（见主流程），此处只校验车定义文件存在
  # 旧名兼容：指向 dimsweep-arms.conf 的自动落到旧名文件上
  if [ -n "$CARS_FILE" ] && [ ! -s "$CARS_FILE" ]; then
    _leg="${CARS_FILE%cars.conf}arms.conf"
    if [ "$_leg" != "$CARS_FILE" ] && [ -s "$_leg" ]; then
      echo "提示: 车定义文件 $CARS_FILE 不存在，改用旧名 $_leg" >&2
      CARS_FILE="$_leg"
    fi
  fi
  [ -s "$CARS_FILE" ] || die "排程模式需要车定义文件: CARS_FILE=$CARS_FILE"
  # CONFIGS 也必须清空：else 分支留的单配置哨兵 ("") 还在，而 CONF_LABELS 是空的
  # → 写 PACKAGES.txt 时 "${CONF_LABELS[0]}" 在 set -u 下报 unbound variable
  #   （起跑即崩，DRY 走不到这行所以发现不了）。
  CONFIGS=(); CONF_LABELS=(); CONF_PKGS=()
elif [ -n "${CONFIGS[0]:-}" ]; then
  _seen="|"
  for _ln in "${CONFIGS[@]}"; do
    apply_config "$_ln"
    case "|$_seen|" in *"|$CAR_LABEL|"*) die "CAR_LABEL 重复: $CAR_LABEL（多配置时标签必须互不相同，否则场次名冲突）";; esac
    _seen="$_seen$CAR_LABEL|"
    CONF_LABELS+=("$CAR_LABEL"); CONF_PKGS+=("$ENGINE_PKG")
  done
else
  resolve_engine
  CONF_LABELS=("$CAR_LABEL"); CONF_PKGS=("$ENGINE_PKG")
fi

# 轮内右旋（对配置索引；r1 原序，之后每轮右移一位——配对交错，摊薄时序效应）
order_for_round() { # <round> → 输出索引序列
  # ⚠️ 同行 local 不得引用同行变量（见 ibs_ready_wait 注释）——off 必须单独一行
  local n=${#CONFIGS[@]} i
  local off=$(( ($1 - 1) % n ))
  for i in $(seq 0 $((n-1))); do echo $(( (i - off + n) % n )); done
}

# 车名 → 配置行（排程模式用；车定义文件每行 "<车名> <配置行...>"）
car_config() { # <车名>
  local want="$1" name line
  [ -f "${CARS_FILE:-}" ] || return 0
  while read -r name line; do
    [ -n "${name:-}" ] || continue
    case "$name" in '#'*) continue;; esac
    if [ "$name" = "$want" ]; then printf '%s' "$line"; return 0; fi
  done < "$CARS_FILE"
  return 0
}

# 排程模式：这一轮会用到的**所有**安装包（引擎包 + 用户自管 JDK 包 + 修补版 agent）。
# 排程是推进时逐条注册配置的，起跑那一刻还不知道将来用哪些包；车定义文件是静态的，
# 按它把包全列出来才有可回溯性。
schedule_packages() {
  local car cfg
  while read -r car cfg; do
    [ -n "${car:-}" ] || continue
    case "$car" in '#'*) continue;; esac
    # 用子 shell 调 apply_config：复用真正的包名解析逻辑（resolve_engine），
    # 又不污染当前进程的 ENGINE_*/CAR_LABEL
    ( apply_config "$cfg" >/dev/null 2>&1
      [ -n "${ENGINE_PKG:-}" ] && printf '%s\n' "$ENGINE_PKG"
      case "${JDK_DIR:-}" in
        */jdk-26) printf '%s\n' "$PKG_DIR/openjdk-26_linux-x64_bin.tar.gz";;
        */jdk-27) printf '%s\n' "$PKG_DIR/openjdk-27_linux-x64_bin.tar.gz";;
      esac )
  done < "$CARS_FILE"
  # 修补版 agent（JDK 26/27 车会把它换进引擎目录）
  [ -f "$ROOT/dsfix/agent-patched-1.18.15.jar" ] && printf '%s\n' "$ROOT/dsfix/agent-patched-1.18.15.jar"
  [ -f "$PKG_DIR/agent-patched-1.18.15.jar" ] && printf '%s\n' "$PKG_DIR/agent-patched-1.18.15.jar"
  return 0
}

# 排程文件 → 有效行（去 # 行内注释、去首尾空白、去空行）
# ⚠️ 必须整行剥注释：只剥第一字段的话，行尾注释会落进 est 字段，
#    随后 $(( est*60 )) 遇非数字直接报错，窗底守卫就失效了。
schedule_lines() {
  [ -f "${SCHEDULE_FILE:-}" ] || return 0
  sed 's/#.*//' "$SCHEDULE_FILE" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -v '^$'
}

# 车定义行 → 引擎名（只读，不 apply_config，不改任何状态）
car_engine() { printf '%s' "$1" | grep -o 'ENGINE_NAME=[^ ]*' | cut -d= -f2 | tr 'A-Z' 'a-z'; }

# 排程起跑前复核「车的引擎 × 变体」兼容性 —— fail-fast。
# ZSTD / index.source_reuse 是 Easysearch 专有设置，ES 上 create-index 直接拒
# （unknown setting）→ 不拦要等建索引那步才炸，白烧一台机器的建库时间。
# 排程模式是用户**逐行写死**的组合 → 有不兼容就 die（在 campaign 起跑前）。
check_track_engine_compat() {
  local car tk rnd dr est cfg eng allow bad=0
  while read -r car tk rnd dr est; do
    [ -n "$car" ] || continue
    allow=$(track_engines "$tk")
    [ -n "$allow" ] || continue
    cfg=$(car_config "$car")
    eng=$(car_engine "$cfg")
    [ -n "$eng" ] || continue
    if [ "$eng" != "$allow" ]; then
      echo "  不兼容: 车 $car（引擎 $eng）× 变体 $tk（该变体只支持 $allow）" >&2
      bad=1
    fi
  done < <(schedule_lines)
  [ "$bad" = 0 ] || die "排程里有引擎与变体不兼容的组合（见上）—— 起跑前拦下，别白跑"
  return 0
}


if [ "$DRY" = 1 ] || [ "${WHAT:-}" = "--dry-run" ]; then
  echo "== DRY RUN 排程（不执行任何操作） =="
  if [ -n "${SCHEDULE_FILE:-}" ]; then
    echo "排程模式: SCHEDULE_FILE=$SCHEDULE_FILE"
    echo "车定义:   CARS_FILE=$CARS_FILE"
    [ "${DS_HOURS:-0}" -gt 0 ] && echo "窗底守卫: ${DS_HOURS}h（窗底留 20min 余量）"
    # 引擎 × 变体兼容性先查：ZSTD / source_reuse 是 Easysearch 专有，ES 上
    # create-index 会拒。dry-run 就报出来，别等真跑起来才炸。
    check_track_engine_compat
    echo "| # | 车 | 赛道 | 轮 | recall | est(min) | 引擎身份 |"
    echo "|---|---|---|---|---|---|---|"
    n=0
    while read -r car tk rnd do_recall est; do
      [ -n "$car" ] || continue
      n=$((n+1))
      _c=$(car_config "$car")
      if [ -z "$_c" ]; then
        printf "| %d | %s | %s | %s | %s | %s | **车未定义** |\n" "$n" "$car" "$tk" "$rnd" "$do_recall" "${est:-?}"
        continue
      fi
      apply_config "$_c"
      printf "| %d | %s | %s | %s | %s | %s | %s %s (%s) |\n" "$n" "$car" "$tk" "$rnd" "$do_recall" "${est:-?}" "$ENGINE_NAME" "$FULL_VER" "$CAR_LABEL"
    done < <(schedule_lines)
    echo "共 $n 场。DRY=1/--dry-run 不会创建 campaign、不会触碰集群。"
    exit 0
  fi
  echo "tracks: ${TRACKS[*]}  rounds: $ROUNDS  configs: ${#CONFIGS[@]}"
  echo "| # | 配置 | 引擎身份 | 赛道 | 轮 |"
  echo "|---|---|---|---|---|"
  n=0
  for tk in "${TRACKS[@]}"; do
    for r in $(seq 1 "$ROUNDS"); do
      for idx in $(order_for_round "$r"); do
        n=$((n+1))
        apply_config "${CONFIGS[$idx]}"
        printf "| %d | %s | %s %s | %s | r%d |\n" "$n" "$CAR_LABEL" "$ENGINE_NAME" "$FULL_VER" "$tk" "$r"
      done
    done
  done
  echo "共 $n 场。DRY=1/--dry-run 不会创建 campaign、不会触碰集群。"
  exit 0
fi

# campaign 名前缀默认 jdk3，可用 CAMP_PREFIX 覆盖（车名进 campaign 维度，
# 否则按批次过滤时看板上会认错车）。
# CAMP_ID 显式指定 = 复用同一 campaign 目录 → STATE.txt 断点续跑（排程模式用）
CAMP_ID="${CAMP_ID:-${CAMP_PREFIX:-jdk3}-$(date +%Y%m%d-%H%M%S)}"
CAMP_DIR="$RESULTS_ROOT/$CAMP_ID"
mkdir -p "$CAMP_DIR/results" "$CAMP_DIR/logs"
PROG="$CAMP_DIR/PROGRESS.txt"; RACES="$CAMP_DIR/RACES.md"
STATE="$CAMP_DIR/STATE.txt"
PROGSTEP() { echo "[$(date '+%F %T')] $*" | tee -a "$PROG"; }

# 中止收尾：进度台判"已结束"只认 PROGRESS.txt 的 "campaign 结束: N/M 场有效" 行。
# EXIT trap 在没写过自然结束行时补一条（N/M 取当前计数，不编数）—— 否则被
# TERM/INT/HUP 或 die 掉的 campaign 永远显示"运行中"。
# KILL -9 拦不住，那条路由 stop-campaign.sh → close-campaign.sh 兜底。
# RUNNER.pid 给 close-campaign.sh 判活；退出即删，免得 pid 复用误判。
echo $$ > "$CAMP_DIR/RUNNER.pid"
CAMPAIGN_CLOSED=0; ABORT_REASON=''
_close_on_exit() {
  local rc=$?
  rm -f "$CAMP_DIR/RUNNER.pid"
  [ "$CAMPAIGN_CLOSED" = 1 ] && return 0
  [ -f "$PROG" ] || return 0
  echo "[$(date '+%F %T')] campaign 结束: ${okcnt:-0}/${total:-0} 场有效（中止: ${ABORT_REASON:-退出码 $rc}；续跑 CAMP_ID=${CAMP_ID}）" >> "$PROG"
}
trap _close_on_exit EXIT
trap 'ABORT_REASON=SIGTERM; exit 143' TERM
trap 'ABORT_REASON=SIGINT; exit 130' INT
trap 'ABORT_REASON=SIGHUP; exit 129' HUP

# ---------- 引擎映射（ibs 承载部署；路径/服务按 ENGINE_NAME 取） -----------------
EZ_DIR="$IBS_BASE/easysearch"
ES_DIR="$IBS_BASE/elasticsearch"
OS_DIR="$IBS_BASE/opensearch"

eng_dir_of()   { case "$1" in elasticsearch) echo "$ES_DIR";; easysearch) echo "$EZ_DIR";; opensearch) echo "$OS_DIR";; *) return 1;; esac; }
svc2_of()      { case "$1" in elasticsearch) echo elasticsearch;; easysearch) echo easysearch;; opensearch) echo opensearch;; *) return 1;; esac; }
ip_node_names_of(){ case "$1" in elasticsearch) echo "${VM_NODES_ES[@]}";; opensearch) echo "${VM_NODES_OS[@]}";; *) echo "${VM_NODES_EZ[@]}";; esac; }

# ---------- ibs 生命周期 ------------------------------------------------------
# 依赖 ibs 副本含两项修复：af01883（gate 按 version-core 精确断言版本）、
# bedf4f1（wipe 先停净再删 data —— 停着删会把主分片搞成永久 UNASSIGNED）。
ibs_selfcheck() {
  local miss=""
  if git -C "$IBS_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # git 副本：按提交祖先判定（不依赖源码字符串 —— 对方改注释/变量名不会误判）
    git -C "$IBS_ROOT" merge-base --is-ancestor af01883 HEAD 2>/dev/null \
      || miss="$miss af01883(gate-version)"
    git -C "$IBS_ROOT" merge-base --is-ancestor bedf4f1 HEAD 2>/dev/null \
      || miss="$miss bedf4f1(wipe-stop-first)"
  else
    # 非 git 副本（scp 同步的裸目录）：回退到能力字符串探测
    grep -q "expected_core" "$IBS_ROOT/ops/status.py" || miss="$miss af01883(gate-version)"
    grep -q "STILL_RUNNING" "$IBS_ROOT/ops/clean.py"  || miss="$miss bedf4f1(wipe-stop-first)"
  fi
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
    opensearch)    tflag="-t opensearch";    vflag="-o $FULL_VER";;
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

# 确保引擎已安装且 build 正确。以 ip.txt 第4列完整 build 号为部署状态单一事实来源
# （REST 只报 version core，无法区分 build 号 → live 探测对 build 差异必然漏判）。
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
# 首次换装自动把当前 JDK 归档为 jdk.bundled（已有归档 jdk.bundled-25 也复用）；按
# jdk.path.stamp 幂等跳过；java -version 校验失败回退 jdk.old。
# 不设硬性卡点——身份元数据 = CAR_LABEL + sidecar 逐节点实测 java -version
# （results/<fid>-<ph>-sidecar.json）。
ensure_jdk() {
  local ip dir="$EZ_DIR" arch want
  [ "$ENGINE_NAME" != easysearch ] && return 0  # ES/OS 用自带 JDK
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
        # 虚机上没有这个 JDK：/opt/jdks/jdk-<ver> 约定且本地有安装包 → 直接装（幂等）
        if ! provision_jdk "$ip"; then
          log "  $ip: JDK_DIR 不存在或缺 bin/java: $JDK_DIR（也没法从 $PKG_DIR 自动安装）"; return 1
        fi
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

# ---------- 赛道准备（定义定版在 tracks/，这里只补 git 收不了的产物）----------

# 赛道定义（index.json / challenges / operations / track.json）全部来自仓库 tracks/，
# 运行前只补三类产物：① 语料软链（GB 级）② 查询集生成（hv1024 系）③ .offset 断点处理。
# 压缩变体不生成目录：压缩档与 source_reuse 由 --track-params 传给定版 index.json
# 的条件块（{% if index_codec is defined %} / {% if source_reuse ... %}）。
link_corpus() { # <赛道目录> <语料文件名> [候选源...]
  local d="$1" f="$2"; shift 2
  local src="" c
  for c in "$@"; do [ -e "$c" ] && { src="$c"; break; }; done
  if [ -z "$src" ]; then
    log "  提示: 语料 $f 没有本地副本（track.json 里有 base-url 时 rally 会自行下载）"
    return 0
  fi
  [ "$(readlink -f "$d/$f" 2>/dev/null)" = "$(readlink -f "$src")" ] && return 0
  ln -sf "$src" "$d/$f"
  log "  语料软链: $f → $src"
}

# 按 track.json 里声明的 source-file 物化语料软链（软链，不复制）
prepare_track_corpus() { # <赛道目录>
  local d="$1" f
  for f in $(grep -o '"source-file"[[:space:]]*:[[:space:]]*"[^"]*"' "$d/track.json" 2>/dev/null \
             | sed 's/.*"\([^"]*\)"$/\1/'); do
    [ -e "$d/$f" ] && continue
    case "$f" in
      documents-1024.json)
        [ -n "${HV_CORPUS:-}" ] && [ -f "${HV_CORPUS}" ] \
          || die "$(basename "$d"): 需要 HV_CORPUS=1M×1024 维语料 JSON 路径"
        link_corpus "$d" "$f" "$HV_CORPUS";;
      documents.json.bz2)
        link_corpus "$d" "$f" "$HOME/.rally/benchmarks/data/nested/documents.json.bz2";;
      documents-2.json.bz2)
        link_corpus "$d" "$f" "$HOME/.rally/benchmarks/data/geonames/documents-2.json.bz2";;
      *) link_corpus "$d" "$f";;
    esac
  done
}

# hv1024 系查询集：官方 queries.json.bz2 前 2000 条（与 96 维 s3 赛道条数对齐）。
# queries.json 不进 git（.gitignore）→ 缺失时按需生成。
# ⚠️ 已有 queries.json 就**直接复用**（不重生成）：truth.jsonl / queries-recall.jsonl
# 这两份 recall 真值是按同一批查询算的 → 换一份 queries 就对不上，recall 数字作废。
prepare_queries() { # <赛道目录>
  local d="$1" q="$d/queries.json" n=2000
  grep -q 'queries.json' "$d/track.py" 2>/dev/null || return 0
  if [ ! -s "$q" ]; then
    [ -n "${HV_QUERIES_BZ2:-}" ] && [ -f "${HV_QUERIES_BZ2}" ] \
      || die "$(basename "$d"): 缺 queries.json，且未设 HV_QUERIES_BZ2=官方 queries.json.bz2 路径"
    bzcat "$HV_QUERIES_BZ2" | head -n "$n" > "$q.tmp" && mv "$q.tmp" "$q" \
      || { rm -f "$q.tmp"; die "$(basename "$d"): 查询集生成失败"; }
    log "  查询集已生成（$n 条）: $q"
  fi
  local qd; qd=$(python3 -c "import json;print(len(json.loads(open('$q').readline())))" 2>/dev/null || echo 0)
  [ "$qd" = "1024" ] || die "$(basename "$d"): 查询向量维度 $qd ≠ 1024"
}

prepare_tracks() { # 本场用到的赛道：物化语料/查询集 + 断点处理
  local tk d
  for tk in "${TRACKS[@]}"; do
    d=$(track_dir "$tk")
    [ -d "$d" ] || die "赛道目录不存在: $d（$tk）
  多半是仓库没同步全 —— 同步时记得带上 tracks/（scp -r tracks/ <压测机>:/opt/raceway/）
  若这条赛道的定义还没进 git，见 tracks/README.md「还没切完的部分」"
    prepare_track_corpus "$d"
    prepare_queries "$d"
    case "$tk" in
      nested*)
        # nested 语料与 rally 缓存共享 ingest 断点（续跑状态一致，别重复灌 1120 万条）
        local off="$HOME/.rally/benchmarks/data/nested/documents.json.bz2.offset"
        [ -e "$off" ] && ln -sf "$off" "$d/documents.json.bz2.offset";;
      *)
        # 其余赛道：清掉 .offset 断点（rally 会从旧断点续传 → 写段计量失真）
        rm -f "$d"/documents-*.offset "$d"/*.offset 2>/dev/null;;
    esac
  done
}

# ds-*（维度扫描）赛道：语料软链修链 + 维度/条数/字节/查询维数自检。
# ds-* 的定义还没进 git（语料与真值在 $ROOT/corpora-ds/），软链断了就按同名在
# corpora-ds 里找回来。语料自检是"这批数据的语料可信"的唯一凭据，记录落进 campaign 台账。
prepare_ds_tracks() { # <赛道名...>（只处理 ds-*；混跑时其他赛道直接跳过）
  local CORPORA_DS="${CORPORA_DS:-$ROOT/corpora-ds}"
  local tk d dims f base cand want_n want_b srcf corpus real_b qdims got
  [ -d "$CORPORA_DS" ] || die "语料目录不存在: $CORPORA_DS（可用 CORPORA_DS= 指定）"
  for tk in "$@"; do
    case "$tk" in ds-*) ;; *) continue;; esac   # 混跑时非 ds 赛道不归这里管
    d="$ROOT/tracks/$tk"
    [ -d "$d" ] || die "赛道目录不存在: $d"
    dims=$(track_dims "$tk") || die "未知赛道名: $tk"

    # ① 修链：软链断了就在 corpora-ds 里按同名找回来
    for f in "$d"/*.json "$d"/*.jsonl; do
      [ -L "$f" ] || continue
      [ -e "$f" ] && continue                      # 链还通，不动
      base=$(basename "$(readlink "$f")")
      cand="$CORPORA_DS/$base"
      if [ -e "$cand" ]; then
        ln -sfn "$cand" "$f"
        log "  $tk: 修复软链 $(basename "$f") → $cand"
      else
        die "$tk: 软链断了且语料目录里找不到 $base（$f → $(readlink "$f")）"
      fi
    done

    # ② 清 .offset 断点（rally 会从旧断点续传，write 计量失真）
    for f in "$d"/*.offset; do [ -e "$f" ] || continue; rm -f "$f"; done

    # ③ 自检：维度 / 条数 / 字节 / 查询维数
    got=$(grep -o '"dims"[[:space:]]*:[[:space:]]*[0-9]*' "$d/index.json" | head -1 | grep -o '[0-9]*$')
    [ "$got" = "$dims" ] || die "$tk: index.json dims=$got ≠ 赛道维度 $dims"
    want_n=$(grep -o '"document-count"[[:space:]]*:[[:space:]]*[0-9]*' "$d/track.json" | head -1 | grep -o '[0-9]*$')
    want_b=$(grep -o '"uncompressed-bytes"[[:space:]]*:[[:space:]]*[0-9]*' "$d/track.json" | head -1 | grep -o '[0-9]*$')
    srcf=$(grep -o '"source-file"[[:space:]]*:[[:space:]]*"[^"]*"' "$d/track.json" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    corpus="$d/$srcf"
    [ -e "$corpus" ] || die "$tk: 语料不可达: $corpus"
    real_b=$(stat -Lc '%s' "$corpus")
    [ -n "$want_b" ] && [ "$want_b" != "$real_b" ] && die "$tk: 语料字节数漂移 $real_b ≠ track.json 声明 $want_b"
    qdims=$(head -1 "$d/queries.json" 2>/dev/null | python3 -c 'import sys,json; a=json.load(sys.stdin); print(len(a) if isinstance(a,list) else len(a.get("emb",[])))' 2>/dev/null)
    [ "$qdims" = "$dims" ] || die "$tk: 查询向量维数 $qdims ≠ 赛道维度 $dims"
    for f in truth.jsonl queries-recall.jsonl; do
      [ -e "$d/$f" ] || die "$tk: $f 不可达（recall 探针依赖它）"
    done
    log "  $tk: 自检通过 dims=$dims docs=${want_n:-?} bytes=$real_b"
    [ -n "${CAMP_DIR:-}" ] && [ -f "$CAMP_DIR/PACKAGES.txt" ] && \
      printf '%s: 自检通过 dims=%s docs=%s bytes=%s corpus=%s\n' \
        "$tk" "$dims" "${want_n:-?}" "$real_b" "$corpus" >> "$CAMP_DIR/PACKAGES.txt"
  done
  # ④ esrally info 自检（挑战名/操作名能解析）
  for tk in "$@"; do
    case "$tk" in ds-*) ;; *) continue;; esac
    local out; out=$("$RALLY" info --track-path="$ROOT/tracks/$tk" 2>&1) || {
      echo "$out" | tail -15
      die "$tk: esrally info 失败"
    }
    log "  $tk: esrally info 通过"
  done
}

# ds-*（维度扫描）赛道：目录固定在 $ROOT/tracks/<名字>，索引名 vectors
# 维度 → 写入超时：≥1536 维给 14400s，其余 10800s
#
# 变体的索引名 / 超时 / 维度都沿用**基础赛道**（变体只改存储设置，不改索引语义）。
# 变体 id ↔ 目录 ↔ 压缩/sr/引擎 的推导在 vm-bench-lib.sh（与编排台同一套规则）。
track_dims() { local b; b=$(track_base "$1"); case "$b" in ds-g1920) echo 1920;; ds-j2048) echo 2048;; ds-j1536) echo 1536;; ds-j1024) echo 1024;; ds-g960) echo 960;; ds-j768) echo 768;; ds-j512) echo 512;; *) return 1;; esac; }
track_tmo_w() { local b; b=$(track_base "$1"); case "$b" in
  geonames-upsert) echo "${TIMEOUT_UPSERT_W:-$TIMEOUT_GEO_W}";;
  geo) echo "$TIMEOUT_GEO_W";;
  hnsw) echo "$TIMEOUT_HNSW_W";;
  hv1024) echo "$TIMEOUT_HV1024_W";;
  hv1024x*) echo "$TIMEOUT_HV1024_W";;
  nested) echo "$TIMEOUT_NESTED_W";;
  ds-*) local dm; dm=$(track_dims "$1") || { echo "$TIMEOUT_DS_W"; return; }
        if [ "$dm" -ge 1536 ]; then echo "$TIMEOUT_DS_W_HI"; else echo "$TIMEOUT_DS_W"; fi;;
 esac; }
track_tmo_q() { local b; b=$(track_base "$1"); case "$b" in geonames-upsert) echo "${TIMEOUT_UPSERT_Q:-$TIMEOUT_GEO_Q}";; geo) echo "$TIMEOUT_GEO_Q";; hnsw) echo "$TIMEOUT_HNSW_Q";; hv1024|hv1024x*) echo "$TIMEOUT_HV1024_Q";; nested) echo "$TIMEOUT_NESTED_Q";; ds-*) echo "$TIMEOUT_DS_Q";; esac; }

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
      vm "$ip" "VERDICT_STEAL_MAX='$VERDICT_STEAL_MAX' VERDICT_FOREIGN_MAX='$VERDICT_FOREIGN_MAX' $MON_DIR/load-monitor.sh summary $MON_DIR/mon-$fid.csv" 2>/dev/null; } \
      > "$mondir/$fid-${VM_NAMES[$k]}-summary.txt" 2>/dev/null
    vm "$ip" "rm -f $MON_DIR/mon-$fid.csv" >/dev/null 2>&1
  done
  # PROGRESS 里的监控摘要：只报三节点最坏的 steal_max（逐节点明细在
  # monitors/*-summary.txt）
  local worst
  worst=$(grep -ho 'steal_max=[0-9.]*' "$mondir/$fid"-*-summary.txt 2>/dev/null \
          | cut -d= -f2 | sort -g | tail -1)
  PROGSTEP "  监控摘要 $fid: steal_max=${worst:-无数据}%(三节点最坏)"
}

env_summary_csv() { # campaign 收尾：把各场 summary 拍平成一张 CSV（benchmark-result-env/benchmark-result-metric 可直接吃）
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

env_fingerprint_raw() { # campaign 起跑一次 —— 静态环境指纹落 raw/，供每场 sidecar 引用（benchmark-result-env 台账源）
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
  log "  环境指纹已抓取: $FP_DIR（benchmark-result-env 台账源）"
}

race_metric_ok() { # <fid> <w|q> —— 断言 race.json 真有非 null 的吞吐指标
  # rally rc=0 不等于有数据：写赛若整段落在 warmup 里，op_metrics 的 throughput
  # 全为 null，场会被静默判 VALID。
  # race.json 缺失（raceId 没解析出来 / race store 没落文件）= 零机读数据，
  # 同样不能放行，必须按断言失败计。
  local f="$CAMP_DIR/results/$1-$2-race.json"
  if [ ! -s "$f" ]; then
    log "  警告: race.json 未落档 $1-$2，指标断言按失败计"
    return 1
  fi
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
if phase == "wq":
    # 单挑战一场里写段+查段都有：写段按 w 规则（docs/s 吞吐全有 median），
    # 整场按 q 规则（过半 op 有吞吐）
    bulk = [m for m in om if "docs/s" in ((m.get("throughput") or {}).get("unit") or "")] or om
    w_ok = bool(bulk) and all(has_median(m) for m in bulk)
    good = sum(1 for m in om if has_median(m))
    sys.exit(0 if (w_ok and good * 2 >= len(om)) else 1)
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
  case "$ph" in w) ch="write-only";; q) ch="query-only";; *) ch="full";; esac
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
  # EasySearch 不认 _nodes/stats/<metric> 形式（会 400）→ 抓不带 metric 名的全量。
  # 缺这一路的后果：线程池是否打爆、缓存命中、引擎侧累计查询耗时全部无从回答 ——
  # 饱和与限流在数据上不可区分。
  curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 25 \
    "https://${VM_IPS[0]}:$HTTP_PORT/_nodes/_all/stats" > "$d/nodes-stats-full.txt" 2>&1 || true
  for ip in "${VM_IPS[@]}"; do vm "$ip" "$dir/jdk/bin/java -version 2>&1" > "$d/jdk-$ip.txt" 2>/dev/null; done
  # 实测生效的索引 settings（codec 类结论的可证明性来源：烘进赛道 ≠ 集群里真是这个值）
  local sidx; sidx=$(track_index "$tk")
  [ -n "$sidx" ] && curl -sk -u "$ADMIN_USER:$ADMIN_PW" --max-time 25 \
    "https://${VM_IPS[0]}:$HTTP_PORT/$sidx/_settings?flat_settings=true" > "$d/index-settings.txt" 2>&1 || true
  python3 "$LIB_DIR/bench-sidecar.py" --raw "$d" --out "$CAMP_DIR/results/$fid-$ph-sidecar.json" \
    --campaign "$CAMP_ID" --fid "$fid" --phase "$ph" --race-id "$LAST_RACE_ID" \
    --car "$CAR_LABEL" --track "$tk" --challenge "$ch" --round "$rnd" --attempt "$att" \
    --track-dir "$tdir" --track-params "$tpar" --heap "$HEAP" --cores "${EXPECT_CPU_CORES:-}" \
    --pkg "$ENGINE_PKG" --jdk-want "$JDK_WANT_ARG" \
    --env-id "$ENV_ID" \
    || log "  警告: sidecar 生成失败 $fid-$ph（不影响 race）"
}

# ---------- recall 探针（维度扫描 Phase A/B 场）---------------------------------
# 索引在线时打 k10/ef100 与 k100/ef1000，和精确真值比对召回。
# 召回塌了的性能数字不作数（PLAN §一 护栏），所以这两档必须落在结果里。
recall_probe() { # <fid> <track_dir> <track_key>
  local fid="$1" tdir="$2" tk="$3"
  local script="${RECALL_SCRIPT:-/testing/aijiami-perf-test/hv-bench/hv-recall-self.py}"
  [ -f "$script" ] || { log "  警告: recall 探针脚本缺失: $script（跳过）"; return 0; }
  local idx host; idx=$(track_index "$tk"); host="https://${VM_IPS[0]}:$HTTP_PORT"
  local k cand out
  for spec in "10 100" "100 1000"; do
    k="${spec%% *}"; cand="${spec##* }"
    out="$CAMP_DIR/results/recall-$fid-k$k.json"
    if python3 "$script" --host "$host" --user "$ADMIN_USER" --password "$ADMIN_PW" \
         --index "$idx" --queries "$tdir/queries-recall.jsonl" --truth "$tdir/truth.jsonl" \
         --k "$k" --candidates "$cand" --limit "${RECALL_LIMIT:-2000}" --tag "$fid" \
         > "$out" 2> "$CAMP_DIR/logs/recall-$fid-k$k.log"; then
      log "  recall k=$k: $(grep -o '"avg_recall":[^,]*' "$out" | head -1)"
    else
      log "  警告: recall 探针失败 k=$k（见 logs/recall-$fid-k$k.log）"
    fi
  done
}

# ---------- JDK 26/27 车：场前无条件推送修补组合 + JDK 身份断言 -------------------
# 每场都要推：重装/切车会把 config/jvm.options 与 lib 下的 security agent 还原成
# 发行版原版（原版 agent 不支持 Java 25+，原版 jvm.options 的 javaagent 作用域只到 21），
# 不推就等于没修。
#
# 作用域固定，不跟随目标 JDK 主版本：
#   javaagent        → 21:（21 及以上都挂修补版 agent 1.18.15）
#   UseJVMCICompiler → 21-26:（OpenJDK 26+ 默认开 JVMCI 必须关）
#
# ⚠️ 顺序陷阱：UseJVMCICompiler 在 OpenJDK 26/27 上属实验选项，显式设置（哪怕 -XX:- 关掉）
# 也必须先 -XX:+UnlockExperimentalVMOptions 解锁，且解锁要排在它前面。
#
# 追加块必须自带完整三连：解锁 → 关 → 重新锁上，否则 JVM 拒绝启动
# （"VM option 'UseJVMCICompiler' is experimental and must be enabled via
#   -XX:+UnlockExperimentalVMOptions"）。
jdk_fix_apply() { # <目标 JDK 主版本号>
  local major="${1:-25}"
  [ "$ENGINE_NAME" = easysearch ] || return 0
  local fixdir="${JDK_FIX_DIR:-$ROOT/dsfix}"
  local agent="$fixdir/agent-patched-1.18.15.jar"
  [ -f "$agent" ] || agent="$PKG_DIR/agent-patched-1.18.15.jar"
  [ -f "$agent" ] || { log "  警告: 修补版 agent 缺失（$agent），跳过 agent 替换"; }
  # JVMCI 作用域固定止于 26（不跟随目标主版本）：OpenJDK 27 已移除 UseJVMCICompiler，
  #   作用域带上 27 会让 JDK 27 直接报 "Unrecognized VM option 'UseJVMCICompiler'"。
  #   JDK 27 里压根没有 JVMCI 这个概念，行为上与「JVMCI 关」等价，
  #   所以三个车（GraalVM25 / OpenJDK26 / OpenJDK27）的向量加速路径保持一致。
  local jci_hi=26
  # 判定依据 = **bundle 实际是否自带 security agent**，而不是版本号：
  #   (a) 新式 bundle（2.5.1+）：lib/easysearch-security-agent.jar —— 自带且已适配新 JDK，
  #       保持原版，跳过整段修补。
  #   (b) 老式 bundle（2.5.0 线）：lib/easysearch-security-agent-<ver>.jar + -bootstrap- + -policy-，
  #       原版 agent 不支持 Java 25+，需要换 patched agent —— 走下面的修补分支。
  #   (c) **无 agent 的 bundle（2.4.0 线）**：bundle 里根本没有 security agent jar，
  #       官方 jvm.options 也没有 javaagent 行。此时套用 2.5.0 时代的 patched agent 是错的：
  #       patched 1.18.15 只含 org/easysearch.security.agent.*，缺 bootstrap.AgentPolicy
  #       （那部分在 2.5.x bundle 里是独立的 -bootstrap- jar），加载即 NoClassDefFoundError，
  #       服务起不来 → COLD-START-FAIL。正确做法 = 保持官方原版，一行都不改。
  local agent_style
  agent_style=$(vm "${VM_IPS[0]}" "
    if [ -f $EZ_DIR/lib/easysearch-security-agent.jar ]; then echo new;
    elif ls $EZ_DIR/lib/easysearch-security-agent-*.jar >/dev/null 2>&1; then echo old;
    else echo none; fi")
  case "$agent_style" in
    new)
      log "  检测到新式自带 agent（easysearch-security-agent.jar），保持 bundle 原版，跳过修补组合"
      return 0;;
    none)
      # 官方 bundle 自带 JDK 与官方 jvm.options（无 javaagent、无 JVMCI 行）——一律不碰。
      # 唯一动作：确保框架此前推来的 patched agent 被清掉（否则旧补丁残留会让服务起不来）。
      for ip in "${VM_IPS[@]}"; do
        vm "$ip" "rm -f $EZ_DIR/lib/easysearch-security-agent-2.5.0.jar" || true
      done
      log "  bundle 无 security agent（官方原版形态，JVM 修补不适用），保持官方 jvm.options 不变"
      return 0;;
  esac
  local ip
  for ip in "${VM_IPS[@]}"; do
    vm "$ip" "set -e
      f=$EZ_DIR/config/jvm.options
      [ -f \$f ] || { echo 'jvm.options 缺失' >&2; exit 1; }
      # 去掉所有 javaagent 版本作用域行，再写一行 21-:（修补版 agent 覆盖 21+）
      grep -v '^[0-9][0-9-]*:-javaagent:lib/easysearch-security-agent-2.5.0.jar' \$f | grep -v '^[0-9][0-9-]*:.*UseJVMCICompiler' > \$f.new
      printf '21-:-javaagent:lib/easysearch-security-agent-2.5.0.jar\n' >> \$f.new
      printf '21-$jci_hi:-XX:+UnlockExperimentalVMOptions\n' >> \$f.new
      printf '21-$jci_hi:-XX:-UseJVMCICompiler\n' >> \$f.new
      printf '21-$jci_hi:-XX:-UnlockExperimentalVMOptions\n' >> \$f.new
      mv \$f.new \$f
      chown easysearch:easysearch \$f; chmod 640 \$f" || { log "  $ip: 修补 jvm.options 失败"; return 1; }
    if [ -f "$agent" ]; then
      vmPut "$ip" "$agent" "$EZ_DIR/lib/easysearch-security-agent-2.5.0.jar" >/dev/null \
        && vm "$ip" "chown easysearch:easysearch $EZ_DIR/lib/easysearch-security-agent-2.5.0.jar" \
        || { log "  $ip: 推送修补版 agent 失败"; return 1; }
    fi
  done
  log "  JDK 修补组合已推送（javaagent 21-: / UseJVMCICompiler 21-$jci_hi: 带解锁三连；目标 JDK $major）"
}

# JDK 身份断言：三节点实测 java -version 的主版本号必须等于期望（JDK_WANT="26|OpenJDK"）
jdk_gate() { # <JDK_WANT>
  local want="$1" ip v got major
  [ -n "$want" ] || return 0
  major="${want%%|*}"
  for ip in "${VM_IPS[@]}"; do
    v=$(vm "$ip" "$EZ_DIR/jdk/bin/java -version 2>&1 | head -1")
    got=$(printf '%s' "$v" | grep -oE '"[0-9]+(\.[0-9]+)*' | head -1 | tr -d '"' | cut -d. -f1)
    if [ "$got" != "$major" ]; then
      PROGSTEP "  JDK 断言失败 $ip: 期望主版本 $major，实测 '$v'"
      return 1
    fi
  done
  return 0
}

run_field() { # <conf_idx> <track> <round> <seq> [attempt]
  local idx="$1" tk="$2" rnd="$3" seq="$4" att="${5:-1}"
  apply_config "${CONFIGS[$idx]}"     # 换到本场的配置（引擎/JDK/标签）
  local eng="$ENGINE_NAME"
  local fid="${CAR_LABEL}-${tk}-r${rnd}"
  [ "$att" -gt 1 ] && fid="${fid}-try${att}"
  local track; track=$(track_dir "$tk")
  local idx; idx=$(track_index "$tk")
  local rep="$CAMP_DIR/results/$fid-wq.md"
  local rlog="$CAMP_DIR/logs/$fid-wq.log"
  local HOSTS; HOSTS=$(target_hosts)
  local OPTS="use_ssl:true,basic_auth_user:'$ADMIN_USER',basic_auth_password:'$ADMIN_PW',verify_certs:false"
  local tp; tp=$(track_params "$tk")
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
  # 2b) JDK 26/27 车：场前推送修补组合（jvm.options 作用域 + 修补版 agent）并断言 JDK 身份。
  #     放在 ensure_jdk 之后、ibs wipe 之前 —— wipe 会重启服务，配置必须先落盘。
  if [ "$eng" = easysearch ]; then
    local jmajor=25
    case "${JDK_WANT:-}" in [0-9]*\|*) jmajor="${JDK_WANT%%|*}";; esac
    if ! jdk_fix_apply "$jmajor"; then
      echo "| $fid | JDK-FIX-FAIL | precheck=$PCTAG | - |" >> "$RACES"
      PROGSTEP "[$seq] $fid JDK-FIX-FAIL"; return 1
    fi
    if ! jdk_gate "${JDK_WANT:-}"; then
      echo "| $fid | JDK-ASSERT-FAIL | precheck=$PCTAG | - |" >> "$RACES"
      PROGSTEP "[$seq] $fid JDK-ASSERT-FAIL"; return 1
    fi
  fi

  # 3) 冷启动 + 集群卡点（含端口属主）+ 堆/核断言
  ibs_wipe2 "$eng" || { echo "| $fid | COLD-START-FAIL | precheck=$PCTAG | - |" >> "$RACES"; PROGSTEP "[$seq] $fid COLD-START-FAIL"; return 1; }
  ibs_ready_wait "$eng" "$CLUSTER_WAIT_SEC" "$CAMP_DIR/logs/ibs-ready-$fid.log" || {
    echo "| $fid | CLUSTER-NOT-READY | precheck=$PCTAG | - |" >> "$RACES"
    PROGSTEP "[$seq] $fid CLUSTER-NOT-READY（logs/ibs-ready-$fid.log）"; return 1; }
  heap_assert "$eng" "$HEAP" "$(eng_dir_of "$eng")" "$(svc2_of "$eng")" || { echo "| $fid | HEAP-ASSERT-FAIL | precheck=$PCTAG | - |" >> "$RACES"; PROGSTEP "[$seq] $fid HEAP-ASSERT-FAIL（实际堆≠声明 heap:$HEAP）"; return 1; }
  cpu_affinity_assert "$eng" || { echo "| $fid | CPU-ASSERT-FAIL | precheck=$PCTAG | - |" >> "$RACES"; PROGSTEP "[$seq] $fid CPU-ASSERT-FAIL"; return 1; }

  # 4) full race（单挑战：写段 → forcemerge(1) → 查段，一次调用跑完，phase=wq）
  #    merge 的完成等待由赛道自带（force-merge 同步 API / wait-until-merges-finish）。
  derive_full_challenge "$track" || {
    echo "| $fid | RACE-FAIL | precheck=$PCTAG | - |" >> "$RACES"
    PROGSTEP "[$seq] $fid full challenge 派生失败"; return 1; }
  local TMO=$(( $(track_tmo_w "$tk") + $(track_tmo_q "$tk") ))
  monitor_start "$fid-wq" "$eng"   # 单挑战全程一份宿主侧采样（5s，只记录不判定）
  PROGSTEP "[$seq] $fid full race 开始（timeout ${TMO}s）"
  local rc=0
  local ctag=""; [ -n "${EXPECT_CPU_CORES:-}" ] && ctag=",cores:${EXPECT_CPU_CORES}"
  # 压缩与 source_reuse 是**两个独立标签**：入库侧拆成两个维度，任意一维都能单独分组对比。
  # 旧 campaign 的 user_tags.codec 是糅合串（"ZSTD+source_reuse"），入库侧做兼容解析 → 新旧可比。
  local cdc="" _comp _sr
  _comp=$(track_compression "$tk"); _sr=$(track_source_reuse "$tk")
  [ -n "$_comp" ] && cdc=",codec:$_comp"
  [ -n "$_sr" ] && cdc="$cdc,source_reuse:$_sr"
  timeout "$TMO" "$RALLY" race --pipeline=benchmark-only \
    --target-hosts="$HOSTS" --track-path="$track" --challenge=full \
    ${tp:+--track-params="$tp"} --client-options="$OPTS" \
    --user-tags="campaign:$CAMP_ID,engine:$eng,engine_ver:$FULL_VER,car:$CAR_LABEL,track:$tk,round:$rnd,nnodes:$NNODES,heap:$HEAP,jdk:$CAR_LABEL${ctag}${cdc}" \
    --report-format=markdown --report-file="$rep" > "$rlog" 2>&1 || rc=$?
  [ $rc -ne 0 ] && PROGSTEP "[$seq] $fid full race FAILED rc=$rc（尾部: $(tail -2 "$rlog" | head -1)）"
  capture_race_json "$fid" wq "$rlog"
  sidecar_capture "$fid" wq "$tk" "$rnd" "$att" "$track" "$tp"
  monitor_stop "$fid-wq"

  # 5) recall 探针（索引在线时打；召回塌了的性能数字不作数）
  if [ "${CUR_RECALL:-0}" = "1" ] && [ "$rc" -eq 0 ]; then
    PROGSTEP "[$seq] $fid recall 探针开始"
    recall_probe "$fid" "$track" "$tk"
  fi

  # 6) 判定（监控只记录环境事实、不参与成败；成败看 race 结果与指标真实性）
  local t1=$(date +%s)
  local status="VALID" metok=1
  race_metric_ok "$fid" wq || { metok=0; PROGSTEP "[$seq] $fid 指标断言未过（写段吞吐全 null 或查询吞吐过半 null）"; }
  [ $metok -eq 0 ] && status="METRIC-NULL-FAIL"
  [ $rc -ne 0 ]    && status="RACE-FAIL"

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

apply_config "${CONFIGS[0]:-}"      # 排程模式下为空（配置在推进时逐条注册）
load_topology "$ENGINE_NAME"
PROGSTEP "campaign $CAMP_ID 启动: configs=[${CONF_LABELS[*]}] tracks=${TRACKS[*]} rounds=$ROUNDS（轮内右旋配对交错）topology=$NNODES 节点（ip.txt 行 $NODE_RANGE）"
if [ -n "${SCHEDULE_FILE:-}" ] && [ -s "$RACES" ]; then
  # 断点续跑：复用同一 CAMP_ID 时保留既有判定表（追加），否则已完成场会被重跑
  PROGSTEP "断点续跑：沿用已有判定表 $RACES（已 VALID 的场自动跳过）"
else
  echo "# RACES — $CAMP_ID" > "$RACES"
  echo "| 场次 | 状态 | 赛前卡点 | 用时 |" >> "$RACES"
  echo "|---|---|---|---|" >> "$RACES"
fi

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
  if [ -n "${SCHEDULE_FILE:-}" ]; then
    # 排程模式：整份排程内联存档 + 所有车的包指纹
    echo "schedule: $(schedule_lines | tr '\n' ' ')"
    echo "cars: $CARS_FILE"
    schedule_packages | sort -u | while read -r _p; do
      [ -f "$_p" ] && sha256sum "$_p" 2>/dev/null
    done
  else
    echo "configs: ${#CONFIGS[@]}（轮内右旋）"
    for i in "${!CONFIGS[@]}"; do
      echo "  [$i] ${CONF_LABELS[$i]:-?}: ${CONFIGS[$i]:-（顶层环境旋钮）}"
    done
    for p in "${CONF_PKGS[@]}"; do echo "$p"; done | sort -u | while read -r _p; do
      sha256sum "$_p" 2>/dev/null
    done
  fi
} > "$CAMP_DIR/PACKAGES.txt"

env_fingerprint_raw    # benchmark-result-env 台账源（静态部分，每场 sidecar 引用同一份）

# 排程模式：赛道集合从排程文件里收集（prepare/校验按这个集合走）
if [ -n "${SCHEDULE_FILE:-}" ]; then
  TRACKS=()
  while read -r car tk rnd do_recall est; do
    [ -n "$car" ] || continue
    case " ${TRACKS[*]} " in *" $tk "*) ;; *) TRACKS+=("$tk");; esac
  done < <(schedule_lines)
  PROGSTEP "排程赛道集合: ${TRACKS[*]}"
  # 引擎 × 变体兼容性复核（fail-fast，在建 campaign 目录/起跑之前）
  check_track_engine_compat
fi

# hnsw 3 分片变体只在**真要跑 hnsw 族**时才生成（判据见 vm-bench-lib.sh 的 tracks_need_hnsw_prep）：
# 无条件生成时，机器上缺 hnsw 基座定义会让 geo 这类无关赛道也起不来，die 报的却是 hnsw 的事。
if tracks_need_hnsw_prep "${TRACKS[@]}"; then
  prepare_hnsw_s3
  [ -d "$TRACK_HNSW_S3" ] || die "hnsw 3 分片变体准备失败"
fi

# 赛道准备：定义已定版在仓库 tracks/，这里只补 git 不入库的产物（语料软链 / 查询集 / 断点）
prepare_tracks

# 存储参数守卫：定版 index.json 缺条件块 → 压缩档/source_reuse 静默失效
# （各臂跑成同一个，数据看着正常、结论全废）→ 起跑前挡住
for _t in "${TRACKS[@]}"; do track_assert_storage_params "$_t"; done

# 存储设置台账：压缩档 / source_reuse 走 --track-params，逐场可回溯靠这一段 ——
# 参数没落进台账，事后没法解释"这一场的索引设置是什么"
{
  echo "--- 存储设置（--track-params）---"
  for _t in "${TRACKS[@]}"; do
    echo "== $_t  dir=$(track_dir "$_t")  params=$(track_params "$_t")"
  done
} >> "$CAMP_DIR/PACKAGES.txt"

case " ${TRACKS[*]} " in *ds-*)
  prepare_ds_tracks "${TRACKS[@]}"
  for d in "${TRACKS[@]}"; do
    case "$d" in ds-*) [ -d "$ROOT/tracks/$d" ] || die "ds-* 赛道缺失: $ROOT/tracks/$d";; esac
  done
;; esac

total=0; okcnt=0; seqn=0
if [ -n "${SCHEDULE_FILE:-}" ]; then
  # ---- 排程模式：逐行 (车, 赛道, 轮次, recall, est分钟)，高维优先 + 窗底守卫 ----
  DEADLINE=0
  if [ "${DS_HOURS:-0}" -gt 0 ]; then
    DEADLINE=$(( $(date +%s) + DS_HOURS*3600 - 1200 ))   # 窗底留 20min 余量
    PROGSTEP "窗底守卫: ${DS_HOURS}h（截止 $(date -d "@$DEADLINE" '+%F %T') 之前放不下的场次 DEADLINE-SKIP）"
  fi
  [ -f "$STATE" ] || : > "$STATE"
  PROGSTEP "排程模式: $SCHEDULE_FILE（车定义 $CARS_FILE）"
  # ⚠️ 排程输入必须挂到**独立 FD**：循环体里的子命令（ssh / ibs / rally / tar …）全部
  # 继承 stdin，会把排程管道读空（后面的排程行被吃干净 → 循环提前 EOF 收尾）。
  # 修法：排程从 FD 3 读，循环体用 read -u 3；子命令的 stdin 与排程彻底隔离。
  exec 3< <(schedule_lines)
  while read -r -u 3 car tk rnd do_recall est; do
    [ -n "$car" ] || continue
    seqn=$((seqn+1))
    # 断点续跑：STATE 里已有 OK 的场次直接跳过
    if grep -qx "OK $seqn" "$STATE" 2>/dev/null; then
      PROGSTEP "[$seqn] $car-$tk-r$rnd 跳过（STATE 已有 OK $seqn）"
      continue
    fi
    # 窗底守卫：这一场放不下就跳过，继续后面更便宜的场（保证高价值数据先落袋）
    if [ "$DEADLINE" -gt 0 ]; then
      _now=$(date +%s)
      if [ $(( _now + ${est:-0}*60 )) -gt "$DEADLINE" ]; then
        PROGSTEP "[$seqn] $car-$tk-r$rnd DEADLINE-SKIP（est=${est}min，距窗底放不下）"
        echo "SKIP $seqn" >> "$STATE"
        continue
      fi
    fi
    # 车 → 配置行
    _cfg=$(car_config "$car")
    [ -n "$_cfg" ] || die "排程里的车未在 $CARS_FILE 定义: $car"
    _ci=${#CONFIGS[@]}
    CONFIGS+=("$_cfg")
    apply_config "$_cfg"
    CONF_LABELS+=("$CAR_LABEL"); CONF_PKGS+=("$ENGINE_PKG")
    CUR_RECALL=0; [ "$do_recall" = "recall" ] && CUR_RECALL=1
    PROGSTEP "[$seqn] 排程: $car → $tk r$rnd（车标签 $CAR_LABEL，recall=$do_recall，est=${est}min）"
    if run_field_with_retry "$_ci" "$tk" "$rnd" "$seqn"; then
      okcnt=$((okcnt+1)); echo "OK $seqn" >> "$STATE"
    else
      echo "FAIL $seqn" >> "$STATE"
    fi
    total=$((total+1))
  done
  exec 3<&-
else
  for tk in "${TRACKS[@]}"; do
    for r in $(seq 1 "$ROUNDS"); do
      for idx in $(order_for_round "$r"); do
        seqn=$((seqn+1))
        # 变体 × 引擎兼容：不适配的组合**留痕跳过**（与 DEADLINE-SKIP 同语义：
        # 不计入 total，不白跑一场再失败）。排程模式是逐行写死的，那里直接 die。
        _allow=$(track_engines "$tk")
        if [ -n "$_allow" ]; then
          _eng=$(car_engine "${CONFIGS[$idx]:-}")
          if [ -n "$_eng" ] && [ "$_eng" != "$_allow" ]; then
            PROGSTEP "[$seqn] ${CONF_LABELS[$idx]:-?}-$tk-r$r 跳过（引擎 $_eng 不支持该变体，该变体只支持 $_allow）"
            continue
          fi
        fi
        if run_field_with_retry "$idx" "$tk" "$r" "$seqn"; then okcnt=$((okcnt+1)); fi
        total=$((total+1))
      done
    done
  done
fi

env_summary_csv   # monitors/*-summary.txt → ENV-SUMMARY.csv（benchmark-result-env/benchmark-result-metric 可直接吃）
PROGSTEP "campaign 结束: $okcnt/$total 场有效。报告: $CAMP_DIR/results/；汇总: $RACES"
CAMPAIGN_CLOSED=1   # 自然结束行已写，EXIT trap 不再补
if [ -n "${NOTIFY_URL:-}" ]; then
  curl -sk --max-time 10 -X POST -H 'Content-Type: application/json' \
    -d "{\"campaign\":\"$CAMP_ID\",\"valid\":$okcnt,\"total\":$total,\"races\":\"$RACES\"}" "$NOTIFY_URL" \
    >/dev/null 2>&1 && PROGSTEP "通知已发送: $NOTIFY_URL" || PROGSTEP "通知发送失败（不影响结果）"
fi
for ip in "${VM_IPS[@]}"; do vm "$ip" "rm -rf $EZ_DIR/jdk.old" 2>/dev/null; done
./reset-env.sh all >/dev/null 2>&1 || true
PROGSTEP "已停集群并清理数据（安装与 jdk.bundled* 归档保留；下次不带 JDK_DIR 跑会自动回切自带 JDK）"
