#!/usr/bin/env bash
# ============================================================================
# prepare-codec-track.sh —— 生成 geonames 存储压缩对比的三个赛道变体（在压测机上运行，幂等）
#   ./prepare-codec-track.sh
#
# 基座：$TRACK_GEO = $TRACK_GEO（geonames 语料，
#   自带 write-only(warmup 20s)/query-only 挑战，bulk 5000×8 clients）。
#   共享基座**一律不改**，本脚本只做「复制 + 往 index.json settings 里烘一行」。
#
# 为什么要烘进文件而不是 --track-params：
#   基座 index.json 的 index_codec 条件块只能表达 codec，表达不了 index.source_reuse；
#   而 --track-params 传 JSON（含逗号）在 rally 2.12 的解析里不可靠。三个变体结构完全
#   相同、只差这一行 settings，才是同基线可比。
#
# 产物（$ROOT/tracks/）：
#   geo-codec-def      "index.codec": "default"          （LZ4，两侧通用基线）
#   geo-codec-zstd-sr  "index.codec": "ZSTD" + "index.source_reuse": true（Easysearch 专有）
#   geo-codec-bc       "index.codec": "best_compression" （ES 专有，DEFLATE）
#   每个变体附 VARIANT.txt（口径台账）与 create-only 挑战（建索引探针，秒级验证 settings 组合）
#
# codec 只在**新写段**生效 → 每场赛后 force_merge_to_one 是压缩生效的前提，不要绕开。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

SRC="$TRACK_GEO"
[ -d "$SRC" ] || die "基座赛道不存在: $SRC"
[ -f "$SRC/index.json" ] || die "基座缺 index.json"
grep -q '"index.requests.cache.enable": false' "$SRC/index.json" || die "基座 index.json 结构变了，插入点找不到，需人工核对"
grep -q '"warmup-time-period": 20' "$SRC/challenges/default.json" || die "基座 write-only warmup 不是 20s，先确认口径再建变体"

# rally 语料缓存必须已存在（变体目录不放语料，靠同名 corpus 复用缓存）
DATA_DIR="$HOME/.rally/benchmarks/data/geonames"
[ -f "$DATA_DIR/documents-2.json.bz2" ] || die "rally 语料缓存缺 $DATA_DIR/documents-2.json.bz2（跑过一次 geo 赛道即有）"

mk_variant() { # <dir> <codec> <source_reuse:yes|no> <说明>
  local dst="$ROOT/tracks/$1" codec="$2" sr="$3" note="$4"
  rm -rf "$dst"
  mkdir -p "$dst"
  ( cd "$SRC" && tar cf - --exclude='__pycache__' --exclude='documents-*' . ) | tar xf - --strip-components=1 -C "$dst"
  ln -sf "$DATA_DIR/documents-2.json.bz2" "$dst/documents-2.json.bz2" 2>/dev/null
  rm -f "$dst/documents-2.json.bz2.offset"

  local extra=',
    "index.codec": "'"$codec"'"'
  [ "$sr" = yes ] && extra="$extra,
    \"index.source_reuse\": true"
  python3 - "$dst/index.json" "$extra" <<'PY'
import sys
p, extra = sys.argv[1], sys.argv[2]
s = open(p, encoding='utf-8').read()
key = '"index.requests.cache.enable": false'
if s.count(key) != 1:
    sys.exit("插入点 %r 出现 %d 次，放弃" % (key, s.count(key)))
s = s.replace(key, key + extra, 1)
open(p, 'w', encoding='utf-8').write(s)
PY
  [ $? -eq 0 ] || die "烘 settings 失败: $dst"

  cat > "$dst/VARIANT.txt" <<EOF
track 变体: $(basename "$dst")
基座:       $SRC
基座 md5:   $(md5sum "$SRC/index.json" | cut -c1-12)
烘入设置:   index.codec=$codec$([ "$sr" = yes ] && echo ' + index.source_reuse=true')
说明:       $note
挑战:       write-only(暖机 20s) / query-only / create-only(建索引探针)
EOF

  # create-only：只做 delete-index + create-index + 集群健康，用来在真实引擎上验证
  # settings 组合是否被接受（ES 不认 index.source_reuse，探针可在赛前 10 秒暴露）
  cat > "$dst/challenges/create-only.json" <<'JSON'
    {
      "name": "create-only",
      "description": "Preflight probe: create the geonames index with the baked settings and stop.",
      "schedule": [
        { "operation": "delete-index" },
        { "operation": "create-index" },
        { "name": "check-cluster-health",
          "operation": {
            "operation-type": "cluster-health",
            "index": "geonames",
            "request-params": { "wait_for_status": "green", "wait_for_no_relocating_shards": "true" },
            "retry-until-success": true
          } }
      ]
    }
JSON

  if esrally-work info --track-path="$dst" --track-params="number_of_shards:3,number_of_replicas:0" \
       > "/tmp/$(basename "$dst")-info.log" 2>&1; then
    log "变体就绪: $dst（esrally info 校验通过 ✔）"
  else
    log "变体 $dst esrally info 校验失败 ✘，尾部："
    tail -15 "/tmp/$(basename "$dst")-info.log"
    exit 1
  fi
}

mk_variant geo-codec-def     default           no  "LZ4 默认编码，Easysearch 与 ES 共用基线"
mk_variant geo-codec-zstd-sr ZSTD              yes "Easysearch 专有：ZSTD stored fields + _source 复用"
mk_variant geo-codec-bc      best_compression  no  "ES 专有：DEFLATE best_compression"

log "三个 codec 变体已生成于 $ROOT/tracks/"
