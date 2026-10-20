#!/usr/bin/env bash
# ============================================================================
# prepare-nested-track.sh —— 生成 nested 赛道的 3 分片变体（在压测机上运行，幂等）
#   ./prepare-nested-track.sh
#
# 基座：$TRACK_NESTED_BASE（上游 rally nested 赛道，
#   StackOverflow 11,203,029 docs，nested 文档 + randomized 查询 param-source）
#   基座 index.json 已支持 number_of_shards/replicas 参数，且自带 index-only 挑战，
#   缺 query-only —— 本脚本补上，并给写赛固定 max_num_segments=1（与 geo/hnsw 同口径）。
# 产物：$TRACK_NESTED_S3 = $TRACK_NESTED_S3
#   · challenges/write-only.json ：建索引 → 全量 bulk → refresh → forcemerge(1) → 等合并
#   · challenges/query-only.json ：基座 7 个 nested 查询操作，参数原样沿用
#   · 语料以符号链接指向 rally 本地缓存，documents.json.offset 由 rally 首跑自建
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

SRC="$TRACK_NESTED_BASE"
DST="$TRACK_NESTED_S3"
[ -d "$SRC" ] || die "基座赛道不存在: $SRC"
[ -f "$SRC/documents.json" ] || die "基座缺语料链接: $SRC/documents.json"

mkdir -p "$DST/challenges" "$DST/operations"
cp -f "$SRC/track.json" "$SRC/track.py" "$SRC/index.json" "$SRC/queries.csv" "$DST/" 2>/dev/null
cp -f "$SRC/operations/"*.json "$DST/operations/"
cp -f "$SRC/challenges/default.json" "$DST/challenges/" 2>/dev/null
ln -sf "$SRC/documents.json" "$DST/documents.json"
[ -f "$DST/documents.json.offset" ] || ln -sf "$(readlink -f "$SRC/documents.json").offset" "$DST/documents.json.offset"

# ---- write-only：全量写入 + 强制单段 --------------------------------------
# 依 rally docs/track.rst:469「bulk indexing 通常不应定义 time-period，rally 只把所有
# 文档灌完，warmup 之后的每个样本都算测量样本」——故这里删掉基座的 time-period:3600
# （它与 warmup 相加构成任务总时长，靠语料耗尽才终止，语义上纯属多余且会误导窗口计算）。
# 暖机定 25s：上游/文档示例对 bulk 用固定 120s 预跑，其隐含前提是语料要灌好几分钟
# （120/300~900s ≈ 13~40%）；本集群 nested 全量只要 110~135s，120s 就成了 82~109% → 测量
# 窗口归零、throughput=null。按同一比例带取 25s（≈19~23%），既保住"跳过建索引/首批
# merge 瞬态"的本意，又保证有 ~85~110s 的真实测量区间。
cat > "$DST/challenges/write-only.json" <<'JSON'
    {
      "name": "write-only",
      "description": "Indexes the full nested corpus into 3 shards and force-merges to one segment.",
      "schedule": [
        { "operation": "delete-index" },
        { "operation": { "operation-type": "create-index",
                         "settings": {{index_settings | default({}) | tojson}} } },
        { "name": "check-cluster-health",
          "operation": { "operation-type": "cluster-health", "index": "sonested",
            "request-params": { "wait_for_status": "{{cluster_health | default('green')}}",
                                "wait_for_no_relocating_shards": "true" },
            "retry-until-success": true } },
        { "operation": "index-append", "warmup-time-period": 25,
          "clients": {{bulk_indexing_clients | default(4)}},
          "ignore-response-error-level": "{{error_level | default('non-fatal')}}" },
        { "name": "refresh-after-index", "operation": "refresh" },
        { "operation": { "operation-type": "force-merge", "max-num-segments": 1,
                         "request-timeout": 7200 } },
        { "name": "refresh-after-force-merge", "operation": "refresh" },
        { "name": "wait-until-merges-finish",
          "operation": { "operation-type": "index-stats", "index": "_all",
            "condition": { "path": "_all.total.merges.current", "expected-value": 0 },
            "retry-until-success": true, "include-in-reporting": false } }
      ]
    }
JSON

# ---- query-only：沿用基座 7 个查询操作的并发/限速/迭代数 ------------------
cat > "$DST/challenges/query-only.json" <<'JSON'
    {
      "name": "query-only",
      "description": "Runs the nested query set against an already indexed corpus.",
      "schedule": [
        { "operation": "randomized-nested-queries", "clients": 2, "target-throughput": 20,
          "warmup-iterations": 500, "iterations": 1000 },
        { "operation": "randomized-term-queries", "clients": 2, "target-throughput": 25,
          "warmup-iterations": 500, "iterations": 200 },
        { "operation": "randomized-sorted-term-queries", "clients": 2, "target-throughput": 16,
          "warmup-iterations": 500, "iterations": 200 },
        { "operation": "match-all", "clients": 2, "target-throughput": 5,
          "warmup-iterations": 500, "iterations": 200 },
        { "operation": "nested-date-histo", "clients": 2, "target-throughput": 1,
          "warmup-iterations": 100, "iterations": 200 },
        { "operation": "randomized-nested-queries-with-inner-hits_default", "clients": 2,
          "target-throughput": 18, "warmup-iterations": 500, "iterations": 1000 },
        { "operation": "randomized-nested-queries-with-inner-hits_default_big_size", "clients": 2,
          "target-throughput": 16, "warmup-iterations": 500, "iterations": 1000 }
      ]
    }
JSON

log "nested-s3 变体已生成: $DST"
if esrally-work info --track-path="$DST" --track-params="number_of_shards:3,number_of_replicas:0" \
     > /tmp/nested-s3-info.log 2>&1; then
  log "esrally info 校验通过 ✔（挑战: $(grep -oE 'write-only|query-only|nested-search-challenge' /tmp/nested-s3-info.log | sort -u | tr '\n' ' ')）"
else
  log "esrally info 校验失败 ✘ —— 见 /tmp/nested-s3-info.log 尾部："
  tail -15 /tmp/nested-s3-info.log
  exit 1
fi
