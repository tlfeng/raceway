#!/usr/bin/env bash
# ============================================================================
# close-campaign.sh —— 给中途停止的 campaign 补「结束行」收尾
# 运行位置：压测宿主机（campaigns 目录所在机器）
#
# 为什么需要它
#   进度台/历史页判定"已结束"的唯一依据是 PROGRESS.txt 里最后一次 `启动:` 之后
#   出现 `campaign 结束: N/M 场有效`。这一行只有 run-bench.sh 自然跑完才写；
#   被 stop-campaign.sh 杀掉、虚机重启、ssh 掉线、die 退出都不写 —— 这些 campaign
#   会永远显示"运行中"，还会被控制台当成"最新没收尾的"默认选中。
#
# 做什么
#   对没有结束行的 campaign 追加一行**格式合法**的结束行（N/M 取 RACES.md 的实测
#   计数，不编数字），括号里注明是中止收尾；保留 PROGRESS.txt 原 mtime，历史页排序
#   不乱。不碰 RACES.md / STATE.txt / 集群 / 被测引擎 —— 断点续跑不受影响。
#
# 用法
#   ./close-campaign.sh [--dry-run] [--root DIR] [--reason TXT] [--stale-min N] [--force]
#                       [--prefix P | <campaign-id> ...]
#   不带 campaign 参数 = 扫全部没收尾的；--prefix ds2c- 只扫该前缀（stop-campaign.sh 用）。
#
# 活跃保护（绝不给还在跑的 campaign 补结束行）
#   1. 目录里有 RUNNER.pid 且该 pid 是活着的 run-bench.sh → 跳过
#   2. 有 esrally 进程 user-tags 带 campaign:<id> → 跳过
#   3. 旧 campaign 没 pid 文件：宿主机上还有 run-bench.sh 活着，且 PROGRESS.txt
#      最近 --stale-min（默认 30）分钟内有更新 → 视为可能是它，跳过（--force 忽略此条）
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# campaigns 目录：环境变量 RESULTS_ROOT > vm-bench.env 里的 RESULTS_ROOT > ./campaigns
# （env 文件在子 shell 里 source 取值，不污染本脚本；stop-campaign.sh 用 --print-root 问它）
if [ -z "${RESULTS_ROOT:-}" ] && [ -f "$HERE/vm-bench.env" ]; then
  RESULTS_ROOT=$(set +u; . "$HERE/vm-bench.env" >/dev/null 2>&1; printf '%s' "${RESULTS_ROOT:-}")
fi
ROOT="${RESULTS_ROOT:-$HERE/campaigns}"
DRY=0; FORCE=0; STALE_MIN=30; PREFIX=''; REASON='手工收尾'
CIDS=()
usage() { sed -n '3,/^# =\{10,\}$/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)   DRY=1;;
    --force)     FORCE=1;;
    --root)      ROOT="${2:?--root 需要目录}"; shift;;
    --reason)    REASON="${2:?--reason 需要文本}"; shift;;
    --stale-min) STALE_MIN="${2:?--stale-min 需要分钟数}"; shift
                 case "$STALE_MIN" in ''|*[!0-9]*) echo "--stale-min 必须是整数分钟: $STALE_MIN" >&2; exit 1;; esac;;
    --prefix)    PREFIX="${2:?--prefix 需要前缀}"; shift;;
    --print-root) echo "$ROOT"; exit 0;;
    -h|--help)   usage 0;;
    -*)          echo "未知参数: $1" >&2; usage 1;;
    *)           case "$1" in *[!A-Za-z0-9._-]*) echo "campaign id 只能含字母数字._-: $1" >&2; exit 1;; esac
                 CIDS+=("$1");;
  esac
  shift
done
[ -d "$ROOT" ] || { echo "campaigns 目录不存在: $ROOT" >&2; exit 1; }
[ -n "$PREFIX" ] && [ ${#CIDS[@]} -gt 0 ] && { echo "--prefix 与显式 campaign 不能同时给" >&2; exit 1; }

# ---- 可移植小工具（宿主机是 Linux；兼容 macOS 只为本地测试） ----------------------
mtime_of()  { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }
fmt_epoch() { date -d "@$1" '+%F %T' 2>/dev/null || date -r "$1" '+%F %T'; }
restore_mtime() {  # restore_mtime <file> <epoch>
  touch -d "@$2" "$1" 2>/dev/null || touch -t "$(date -r "$2" '+%Y%m%d%H%M.%S')" "$1"
}
pid_cmd() { ps -p "$1" -o command= 2>/dev/null; }
# "runner 活着" = 有 shell 进程在执行 run-bench.sh。pgrep -f 匹配整条命令行，
# vim/tail/less/ssh 命令行里带这个文件名的也会命中，所以再按 comm 过滤只留 shell。
runner_pids() {
  local p
  for p in $(pgrep -f 'run-bench[.]sh' 2>/dev/null); do
    case "$(ps -p "$p" -o comm= 2>/dev/null)" in
      bash|sh|dash|zsh|*/bash|*/sh|*/dash|*/zsh) echo "$p";;
    esac
  done
}
runner_alive() { [ -n "$(runner_pids)" ]; }

# 与 bench-queue.build_campaign 同语义：结束行必须出现在最后一次起跑头之后。
is_ended() {
  awk '/启动:/{h=NR} /campaign 结束:/{e=NR} END{exit !(e>0 && e>h)}' "$1"
}

# 活跃判定；返回 0 = 活着（不能收尾），并在 stdout 给出原因
is_live() {  # is_live <cid> <dir>
  local cid="$1" dir="$2" pid
  if [ -f "$dir/RUNNER.pid" ]; then
    pid=$(tr -dc '0-9' < "$dir/RUNNER.pid")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && pid_cmd "$pid" | grep -q 'run-bench'; then
      echo "RUNNER.pid $pid 仍是活着的 run-bench.sh"; return 0
    fi
    return 1   # 有 pid 文件但进程已不在 → 明确已死
  fi
  if pgrep -f "esrally.*campaign:${cid}," >/dev/null 2>&1; then
    echo "有 esrally 进程带 campaign:$cid"; return 0
  fi
  if [ "$FORCE" != 1 ] && runner_alive; then
    local age=$(( ( $(date +%s) - $(mtime_of "$dir/PROGRESS.txt") ) / 60 ))
    if [ "$age" -lt "$STALE_MIN" ]; then
      echo "宿主机有 run-bench.sh 在跑且进度文件 ${age} 分钟前刚更新（--force 可忽略）"; return 0
    fi
  fi
  return 1
}

races_counts() {  # races_counts <dir> → "valid total"（grep -c 计数为 0 时退出码 1，不能用 || 兜底）
  local r="$1/RACES.md" raw valid
  raw=$(grep -c '^|' "$r" 2>/dev/null); raw=${raw:-0}
  valid=$(grep -cE '^\|[^|]*\|[[:space:]]*VALID[[:space:]]*\|' "$r" 2>/dev/null); valid=${valid:-0}
  echo "$valid $(( raw > 2 ? raw - 2 : 0 ))"
}

close_one() {  # close_one <cid>  → 0 收尾/将收尾, 1 跳过
  local cid="$1" dir="$ROOT/$1" prog why mt valid total line
  prog="$dir/PROGRESS.txt"
  [ -f "$prog" ] || { echo "SKIP  ${cid}：没有 PROGRESS.txt"; return 1; }
  if is_ended "$prog"; then echo "SKIP  ${cid}：已有结束行"; return 1; fi
  if why=$(is_live "$cid" "$dir"); then echo "LIVE  ${cid}：${why}，不动"; return 1; fi
  mt=$(mtime_of "$prog")
  read -r valid total < <(races_counts "$dir")
  line="[$(date '+%F %T')] campaign 结束: $valid/$total 场有效（中止: ${REASON}；最后活动 $(fmt_epoch "$mt")；close-campaign.sh 补写，续跑 CAMP_ID=${cid}）"
  if [ "$DRY" = 1 ]; then
    echo "WOULD $cid  $valid/$total  最后活动 $(fmt_epoch "$mt")"
  else
    printf '%s\n' "$line" >> "$prog" && restore_mtime "$prog" "$mt"
    echo "CLOSE $cid  $valid/$total  最后活动 $(fmt_epoch "$mt")"
  fi
  return 0
}

# ---- 候选 ------------------------------------------------------------------
if [ ${#CIDS[@]} -eq 0 ]; then
  for d in "$ROOT"/"$PREFIX"*/; do
    [ -f "${d}PROGRESS.txt" ] || continue
    cid="${d%/}"; cid="${cid##*/}"
    is_ended "${d}PROGRESS.txt" && continue     # 扫描模式只报没收尾的，省得刷屏
    CIDS+=("$cid")
  done
  [ ${#CIDS[@]} -eq 0 ] && { echo "没有待收尾的 campaign（root=$ROOT${PREFIX:+ prefix=$PREFIX}）"; exit 0; }
fi

n_close=0; n_skip=0
for cid in "${CIDS[@]}"; do
  if close_one "$cid"; then n_close=$((n_close+1)); else n_skip=$((n_skip+1)); fi
done
echo "---- $([ "$DRY" = 1 ] && echo '预演：将收尾' || echo '已收尾') $n_close 个，跳过 $n_skip 个（root=${ROOT}）"
