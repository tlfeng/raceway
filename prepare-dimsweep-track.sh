#!/usr/bin/env bash
# ============================================================================
# prepare-dimsweep-track.sh —— 维度扫描（ds-*）赛道修复 + 自检（幂等）
#
# 背景：ds3 campaign（2026-10-15/16）跑完 23 场后，语料目录从
#   /testing/vm-bench-202610/corpora-ds/   搬到了   /testing/raceway/corpora-ds/
# 但 7 个 ds-* 赛道里的语料/真值软链仍指向旧路径 → 全部 BROKEN，赛道直接跑不了。
# 本脚本：修链 → 清 .offset 断点 → 维度/条数/字节/查询维数自检 → esrally info 自检
#
# 职责边界：只修链与校验，**不生成语料**（语料制作脚本 dimsweep-*.py 已不在树内，
# 语料与真值本体在 $CORPORA_DS 里齐备，修复指向即可）。
#
# 用法：./prepare-dimsweep-track.sh [赛道名...]   # 不给参数 = 全部 7 个
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

CORPORA_DS="${CORPORA_DS:-$ROOT/corpora-ds}"
TRACKS_ROOT="$ROOT/tracks"
ALL_TRACKS=(ds-g1920 ds-j2048 ds-j1024 ds-g960 ds-j768 ds-j512 ds-j1536)
TRACKS=("$@")
[ ${#TRACKS[@]} -eq 0 ] && TRACKS=("${ALL_TRACKS[@]}")

# 自检记录追加到 campaign 的 PACKAGES.txt（起跑时由 run-bench.sh 建好）。
# 语料自检是"这批数据的语料是可信的"的唯一凭据 —— 不留档就只剩一句"当时看着对"。
# 独立跑本脚本时（CAMP_DIR 未设）不落盘。
SELFCHECK_LOG=""
[ -n "${CAMP_DIR:-}" ] && [ -f "$CAMP_DIR/PACKAGES.txt" ] && \
  SELFCHECK_LOG="$(mktemp "${TMPDIR:-/tmp}/dimsweep-selfcheck.XXXXXX")"

# 赛道名 → 维度（ds-<血统><维度>；j=jina 系，g=GIST 系）
track_dims() { case "$1" in
  ds-g1920) echo 1920;; ds-j2048) echo 2048;; ds-j1536) echo 1536;; ds-j1024) echo 1024;;
  ds-g960)  echo 960;;  ds-j768)  echo 768;;  ds-j512)  echo 512;; *) return 1;; esac; }

die() { echo "[prepare-dimsweep] ERROR: $*" >&2; exit 1; }
log() { echo "[prepare-dimsweep] $*"; }

[ -d "$CORPORA_DS" ] || die "语料目录不存在: $CORPORA_DS（可用 CORPORA_DS= 指定）"

for tk in "${TRACKS[@]}"; do
  d="$TRACKS_ROOT/$tk"
  [ -d "$d" ] || die "赛道目录不存在: $d"
  dims=$(track_dims "$tk") || die "未知赛道名: $tk"

  # ---- 1) 修链：软链断了就在 corpora-ds 里按同名找回来 ---------------------
  for f in "$d"/*.json "$d"/*.jsonl; do
    [ -L "$f" ] || continue
    [ -e "$f" ] && continue                      # 链还通，不动
    base=$(basename "$(readlink "$f")")
    cand="$CORPORA_DS/$base"
    if [ -e "$cand" ]; then
      ln -sfn "$cand" "$f"
      log "$tk: 修复软链 $(basename "$f") -> $cand"
    else
      die "$tk: 软链断了且语料目录里找不到 $base（$f -> $(readlink "$f")）"
    fi
  done

  # ---- 2) 清 .offset 断点（rally 会从旧断点续传，write 计量失真）------------
  for f in "$d"/*.offset; do
    [ -e "$f" ] || continue
    rm -f "$f"
    log "$tk: 清除 .offset 断点 $(basename "$f")"
  done

  # ---- 3) 自检：维度 / 条数 / 字节 / 查询维数 ------------------------------
  # index.json 的 dense_vector dims 必须等于赛道维度
  got=$(grep -o '"dims"[[:space:]]*:[[:space:]]*[0-9]*' "$d/index.json" | head -1 | grep -o '[0-9]*$')
  [ "$got" = "$dims" ] || die "$tk: index.json dims=$got ≠ 赛道维度 $dims"

  # track.json 的 document-count / uncompressed-bytes 必须与语料实际一致
  want_n=$(grep -o '"document-count"[[:space:]]*:[[:space:]]*[0-9]*' "$d/track.json" | head -1 | grep -o '[0-9]*$')
  want_b=$(grep -o '"uncompressed-bytes"[[:space:]]*:[[:space:]]*[0-9]*' "$d/track.json" | head -1 | grep -o '[0-9]*$')
  srcf=$(grep -o '"source-file"[[:space:]]*:[[:space:]]*"[^"]*"' "$d/track.json" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
  corpus="$d/$srcf"
  [ -e "$corpus" ] || die "$tk: 语料不可达: $corpus"
  real_b=$(stat -Lc '%s' "$corpus")
  [ -n "$want_b" ] && [ "$want_b" != "$real_b" ] && die "$tk: 语料字节数漂移 $real_b ≠ track.json 声明 $want_b"

  # 查询向量维数必须等于维度（读第一行即可）
  qdims=$(head -1 "$d/queries.json" | python3 -c 'import sys,json; a=json.load(sys.stdin); print(len(a) if isinstance(a,list) else len(a.get("emb",[])))' 2>/dev/null)
  [ "$qdims" = "$dims" ] || die "$tk: 查询向量维数 $qdims ≠ 赛道维度 $dims"

  # 真值与 recall 查询集必须可达（recall 探针要读）
  for f in truth.jsonl queries-recall.jsonl; do
    [ -e "$d/$f" ] || die "$tk: $f 不可达（recall 探针依赖它）"
  done

  log "$tk: 自检通过 dims=$dims docs=${want_n:-?} bytes=$real_b"
  [ -n "$SELFCHECK_LOG" ] && printf '%s: 自检通过 dims=%s docs=%s bytes=%s corpus=%s\n' \
    "$tk" "$dims" "${want_n:-?}" "$real_b" "$corpus" >> "$SELFCHECK_LOG"
done

# ---- 4) esrally info 自检（挑战名/操作名能解析）----------------------------
# esrally-work 会 cd 到自己的工作目录，所以 track-path 必须给绝对路径
for tk in "${TRACKS[@]}"; do
  out=$("$RALLY" info --track-path="$TRACKS_ROOT/$tk" 2>&1) || {
    echo "$out" | tail -15
    die "$tk: esrally info 失败"
  }
  ch=$(echo "$out" | grep -oiE 'Challenge\(s\):.*' | head -1)
  log "$tk: esrally info 通过 ${ch:-}"
  [ -n "$SELFCHECK_LOG" ] && printf '%s: esrally info 通过 %s\n' "$tk" "${ch:-}" >> "$SELFCHECK_LOG"
done

# ---- 5) 自检记录落进 campaign 的 PACKAGES.txt --------------------------------
if [ -n "$SELFCHECK_LOG" ] && [ -s "$SELFCHECK_LOG" ]; then
  {
    echo "--- 语料自检记录 ---"
    cat "$SELFCHECK_LOG"
  } >> "$CAMP_DIR/PACKAGES.txt"
  log "自检记录已追加到 $CAMP_DIR/PACKAGES.txt"
fi
rm -f "$SELFCHECK_LOG" 2>/dev/null

log "完成：${#TRACKS[@]} 个 ds-* 赛道就绪"
