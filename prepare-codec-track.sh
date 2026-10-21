#!/usr/bin/env bash
# ============================================================================
# prepare-codec-track.sh —— 生成**存储压缩 × source_reuse** 的赛道变体
#                            （在压测机上运行，幂等）
#
#   ./prepare-codec-track.sh                    # 默认：geonames 一族的 6 个变体
#   ./prepare-codec-track.sh hnsw-codec-zstd    # 指定变体（任意基础赛道）
#   ./prepare-codec-track.sh ds-j1024-codec-def-sr
#
# 变体 = 基础赛道 + 压缩档 + source_reuse 开关，规范 id = 目录名：
#   <基础赛道id>-codec-<def|zstd|bc>[-sr]
#     geo-codec-def      geo-codec-def-sr
#     geo-codec-zstd     geo-codec-zstd-sr
#     geo-codec-bc       geo-codec-bc-sr
#     hnsw-codec-zstd    ds-j1024-codec-def-sr   …（任意基础赛道都适用）
#
# 为什么要拆成二维（而不是早期那三个变体）
# ------------------------------------------
# 早期三个变体把两个维度糅在**赛道名**里：
#   geo-codec-def      default                    （压缩=default，sr 关）
#   geo-codec-zstd-sr  ZSTD + source_reuse=true   （压缩=ZSTD，sr 开）  ← 两个变量一起动
#   geo-codec-bc       best_compression           （压缩=bc，sr 关）
# 于是"ZSTD 那一档比 default 快/慢"**无法归因**：到底是 ZSTD 带来的，还是
# source_reuse 带来的？两个都变了，归因不了。
#
# 现在按 **压缩 × source_reuse** 正交展开，任意一档的差异都只归因到一个变量：
#   压缩影响：geo-codec-def    ↔ geo-codec-zstd     （sr 都是关）
#             geo-codec-def-sr ↔ geo-codec-zstd-sr  （sr 都是开）
#   sr 影响：  geo-codec-def    ↔ geo-codec-def-sr  （压缩都是 default）
#             geo-codec-zstd   ↔ geo-codec-zstd-sr  （压缩都是 ZSTD）
#
# 而且这两个是**索引级设置**，对任何基础赛道都成立（hnsw/nested/ds-* 同样适用）——
# 变体只改存储设置，不改索引语义：索引名、分片数、超时都沿用基础赛道。
#
# 为什么要烘进文件而不是 --track-params：
#   基座 index.json 的 index_codec 条件块只能表达 codec，表达不了 index.source_reuse；
#   而 --track-params 传 JSON（含逗号）在 rally 2.12 的解析里不可靠。变体结构完全
#   相同、只差这两行 settings，才是同基线可比。
#
# 引擎兼容（据 Easysearch 源码 EngineConfig.INDEX_CODEC_SETTING 与实测）：
#   index.codec 的合法值 = default / best_compression / lucene_default /
#                          Codec.availableCodecs()（含 ZSTD 系列，Easysearch 专有）
#   index.source_reuse   = Easysearch 专有（ES 不认这个设置）
#   → 两侧通用：压缩=default/best_compression 且 sr 关
#   → 仅 Easysearch：压缩=ZSTD（任意 sr）、或任意压缩 + sr 开
#   编排台按这个规则拦不兼容的组合；runner 侧也会在起跑前 fail-fast 复核。
#
# 历史变体名保持不变（geo-codec-def / -zstd-sr / -bc）→ 旧 campaign 的
# 场次名与已入库数据的口径不受影响。短 id gdef/gzstd/gbc 是**历史别名**
# （gzstd 实为 ZSTD+source_reuse），只作兼容输入，见 vm-bench-lib.sh。
#
# 语料不进变体目录（几个 GB）：只把基座的语料**软链**过来。
# 变体目录里只多一份 index.json 的 settings 差异 + VARIANT.txt 台账。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

# 默认生成哪一族（历史数据在 geonames 这族）
DEFAULT_VARIANTS="geo-codec-def geo-codec-def-sr geo-codec-zstd geo-codec-zstd-sr geo-codec-bc geo-codec-bc-sr"

# rally 语料缓存（geonames 基座不带语料文件，语料在 rally 缓存里）
DATA_DIR="$HOME/.rally/benchmarks/data/geonames"

# 变体 id → (基础赛道, 压缩 token, source_reuse)
parse_variant() { # <变体id> → 设 V_BASE/V_TOKEN/V_SR；不认识则 die
  local vid="$1"
  case "$vid" in
    *-codec-*) ;;
    *) die "变体 id 必须形如 <基础赛道>-codec-<def|zstd|bc>[-sr]，得到: $vid";;
  esac
  V_BASE=$(track_base "$vid")
  V_TOKEN=${vid#*-codec-}
  case "$V_TOKEN" in
    *-sr) V_SR=yes; V_TOKEN=${V_TOKEN%-sr};;
    *) V_SR=no;;
  esac
  case "$V_TOKEN" in
    def) V_CODEC=default;;
    zstd) V_CODEC=ZSTD;;
    bc) V_CODEC=best_compression;;
    *) die "变体 id 里的压缩档不认识: $vid（应为 def/zstd/bc，可加 -sr 后缀）";;
  esac
}

# 烘 settings（两种基座形态）
bake_settings() { # <index.json> <codec> <sr:yes|no>
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys
p, codec, sr = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p, encoding='utf-8').read()
# ① 纯 JSON 基座（hnsw/nested/hv1024/ds-* 都是）→ 直接改 settings.index
try:
    d = json.loads(s)
except ValueError:
    d = None
if d is not None:
    idx = d.setdefault("settings", {}).setdefault("index", {})
    idx["index.codec"] = codec
    # source_reuse 只在要开的时候写：ES 根本不认这个 setting，
    # 写 false 也会让 ES 的 create-index 报 unknown setting。
    if sr == "yes":
        idx["index.source_reuse"] = True
    open(p, 'w', encoding='utf-8').write(json.dumps(d, ensure_ascii=False, indent=2))
    sys.exit(0)
# ② 模板基座（geonames 是 Jinja 模板）→ 按锚点插一行
extra = ',\n    "index.codec": "%s"' % codec
if sr == "yes":
    extra += ',\n    "index.source_reuse": true'
key = '"index.requests.cache.enable": false'
if s.count(key) != 1:
    sys.exit("插入点 %r 出现 %d 次，放弃（基座是模板，需人工核对）" % (key, s.count(key)))
s = s.replace(key, key + extra, 1)
open(p, 'w', encoding='utf-8').write(s)
PY
  [ $? -eq 0 ] || die "烘 settings 失败: $1"
}

mk_variant() { # <变体id> <说明>
  local vid="$1" note="$2"
  parse_variant "$vid"
  local base="$V_BASE" codec="$V_CODEC" sr="$V_SR"
  local src dst idxname n=0 f
  src=$(track_dir "$base")
  dst="$ROOT/tracks/$vid"
  [ -d "$src" ] || die "基座赛道不存在: $src（基础赛道 $base）"
  [ -f "$src/index.json" ] || die "基座缺 index.json: $src"
  [ -f "$src/challenges/default.json" ] || die "基座缺 challenges/default.json: $src"

  rm -rf "$dst"
  mkdir -p "$dst"
  ( cd "$src" && tar cf - --exclude='__pycache__' --exclude='documents-*' . ) | tar xf - --strip-components=1 -C "$dst"

  # 语料只软链，不复制（几个 GB）：基座的 documents-* 软链到真实文件
  for f in "$src"/documents-*; do
    [ -e "$f" ] || continue
    ln -sf "$(readlink -f "$f")" "$dst/$(basename "$f")" 2>/dev/null || true
    n=$((n+1))
  done
  if [ "$n" = 0 ] && [ "$base" = geo ]; then
    # geonames 基座不带语料文件，语料在 rally 缓存里（跑过一次 geo 赛道即有）
    [ -f "$DATA_DIR/documents-2.json.bz2" ] || die "rally 语料缓存缺 $DATA_DIR/documents-2.json.bz2"
    ln -sf "$DATA_DIR/documents-2.json.bz2" "$dst/documents-2.json.bz2" 2>/dev/null || true
    rm -f "$dst/documents-2.json.bz2.offset"
  fi

  bake_settings "$dst/index.json" "$codec" "$sr"

  idxname=$(track_index "$base")
  cat > "$dst/VARIANT.txt" <<EOF
变体 id:    $vid
基础赛道:   $base（$src）
基座 md5:   $(md5sum "$src/index.json" | cut -c1-12)
烘入设置:   index.codec=$codec$([ "$sr" = yes ] && echo ' + index.source_reuse=true')
索引名:     $idxname
引擎兼容:   $(track_engines "$vid" || true)$([ -z "$(track_engines "$vid")" ] && echo "两侧通用" || echo "")
说明:       $note
挑战:       write-only / query-only / create-only(建索引探针)
EOF

  # create-only：只做 delete-index + create-index + 集群健康，用来在真实引擎上验证
  # settings 组合是否被接受（ES 不认 index.source_reuse，探针可在赛前 10 秒暴露）
  cat > "$dst/challenges/create-only.json" <<JSON
    {
      "name": "create-only",
      "description": "Preflight probe: create the $idxname index with the baked settings and stop.",
      "schedule": [
        { "operation": "delete-index" },
        { "operation": "create-index" },
        { "name": "check-cluster-health",
          "operation": {
            "operation-type": "cluster-health",
            "index": "$idxname",
            "request-params": { "wait_for_status": "green", "wait_for_no_relocating_shards": "true" },
            "retry-until-success": true
          } }
      ]
    }
JSON

  # track-params 与 run-bench.sh 的口径一致：只有 geo/nested 的分片数走参数
  local tp=""
  case "$base" in geo|nested) tp="number_of_shards:3,number_of_replicas:0";; esac
  if esrally-work info --track-path="$dst" ${tp:+--track-params="$tp"} \
       > "/tmp/$(basename "$dst")-info.log" 2>&1; then
    log "变体就绪: $dst（esrally info 校验通过 ✔）"
  else
    log "变体 $dst esrally info 校验失败 ✘，尾部："
    tail -15 "/tmp/$(basename "$dst")-info.log"
    exit 1
  fi
}

V_BASE=""; V_TOKEN=""; V_SR=""; V_CODEC=""
targets=("$@")
[ ${#targets[@]} -eq 0 ] && read -r -a targets <<< "$DEFAULT_VARIANTS"

for vid in "${targets[@]}"; do
  vid=$(track_canonical "$vid")     # 历史别名 gdef/gzstd/gbc → 规范 id
  case "$vid" in
    geo-codec-def)      note="压缩=default(LZ4) · source_reuse 关 —— 两侧通用基线";;
    geo-codec-def-sr)   note="压缩=default(LZ4) · source_reuse 开 —— 只变 sr，对照 geo-codec-def";;
    geo-codec-zstd)     note="压缩=ZSTD · source_reuse 关 —— 只变压缩，对照 geo-codec-def";;
    geo-codec-zstd-sr)  note="压缩=ZSTD · source_reuse 开 —— 两项都开（历史变体）";;
    geo-codec-bc)       note="压缩=best_compression(DEFLATE) · source_reuse 关 —— 两侧通用";;
    geo-codec-bc-sr)    note="压缩=best_compression(DEFLATE) · source_reuse 开";;
    *) case "$vid" in
         # 注意：-sr 的模式必须排在不带 -sr 的前面 —— "*-codec-zstd*" 也会匹配
         # "…-codec-zstd-sr"（末尾的 * 能吃掉 "-sr"），顺序反了说明文案会写错。
         *-codec-def-sr*) note="压缩=default(LZ4) · source_reuse 开";;
         *-codec-def*) note="压缩=default(LZ4) · source_reuse 关";;
         *-codec-zstd-sr*) note="压缩=ZSTD · source_reuse 开";;
         *-codec-zstd*) note="压缩=ZSTD · source_reuse 关（ZSTD 是 Easysearch 专有压缩）";;
         *-codec-bc-sr*) note="压缩=best_compression(DEFLATE) · source_reuse 开";;
         *-codec-bc*) note="压缩=best_compression(DEFLATE) · source_reuse 关";;
         *) die "变体 id 解析不了: $vid";;
       esac;;
  esac
  mk_variant "$vid" "$note"
done

log "已生成 ${#targets[@]} 个压缩变体于 $ROOT/tracks/"
