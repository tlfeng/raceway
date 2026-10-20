#!/usr/bin/env bash
# ============================================================================
# prepare-hv1024-track.sh —— 生成 1024 维 HNSW 3 分片赛道变体（幂等，在压测机上运行）
#   ./prepare-hv1024-track.sh
#
#   数据来源：官方 esrally 赛道 msmarco-jina-10m-vector（1024 维 float）的本地化
#   版本（202609 hv-bench 转码，1M 条子集）：
#     语料  $HV_CORPUS（env 可覆盖，默认指向本环境落位）
#           （1,000,000 docs × 1024 维 float，字段 docid + emb，22,739,096,214 字节）
#     查询  官方 queries.json.bz2 前 2000 条（条数与 96 维 s3 赛道对齐）
#   产物：  $ROOT/tracks/hv1024-s3（索引名 vectors，3 分片 0 副本，m=32/efC=100）
#
#   write-only 默认 bulk-size 500 × 8 客户端（1024 维单条 ~22KB，沿 Sept hv 实测口径；
#   可用 --track-params="bulk_size:...,bulk_indexing_clients:...,ingest_percentage:..." 覆盖）
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
source ./vm-bench-lib.sh

CORPUS=${HV_CORPUS:?请设置 HV_CORPUS=1M×1024 维语料 JSON 路径（如 /data/hv/corpora/documents-jina-float.json）}
CORPUS_BYTES=22739096214
CORPUS_COUNT=1000000
QUERIES_BZ2=${HV_QUERIES_BZ2:?请设置 HV_QUERIES_BZ2=查询集 queries.json.bz2 路径}
NQUERIES=2000
TRACK_DIR=${TRACK_HV1024:-$ROOT/tracks/hv1024-s3}

[ -f "$CORPUS" ] || die "1024 维语料不存在: $CORPUS"
[ -f "$QUERIES_BZ2" ] || die "官方查询集不存在: $QUERIES_BZ2"
actual_bytes=$(stat -c %s "$CORPUS")
[ "$actual_bytes" = "$CORPUS_BYTES" ] || die "语料字节数漂移: $actual_bytes ≠ $CORPUS_BYTES（track.json 的 uncompressed-bytes 必须一致）"

mkdir -p "$TRACK_DIR/operations" "$TRACK_DIR/challenges"

# .offset 断点文件绝不带入（rally 会从旧断点续传，write 计量失真）
rm -f "$TRACK_DIR/documents-1024.json.offset"
ln -sfn "$CORPUS" "$TRACK_DIR/documents-1024.json"

cat > "$TRACK_DIR/index.json" <<'EOF'
{
  "settings": { "index": { "number_of_shards": 3, "number_of_replicas": 0 } },
  "mappings": {
    "dynamic": "false",
    "_source": { "enabled": false },
    "properties": {
      "docid": { "type": "keyword" },
      "emb": {
        "type": "dense_vector", "element_type": "float", "dims": 1024,
        "index": true, "similarity": "dot_product",
        "index_options": { "type": "hnsw", "m": 32, "ef_construction": 100 }
      }
    }
  }
}
EOF

cat > "$TRACK_DIR/track.json" <<EOF
{% import "rally.helpers" as rally with context %}
{
  "version": 2,
  "description": "High-dimensional (1024-dim float, dot_product) HNSW benchmark, 1M docs, separated write/query with segment checkpoint",
  "indices": [ { "name": "vectors", "body": "index.json" } ],
  "corpora": [
    {
      "name": "dense-vector-1024",
      "documents": [
        { "source-file": "documents-1024.json", "document-count": $CORPUS_COUNT, "uncompressed-bytes": $CORPUS_BYTES }
      ]
    }
  ],
  "operations": [ {{ rally.collect(parts="operations/*.json") }} ],
  "challenges": [ {{ rally.collect(parts="challenges/*.json") }} ]
}
EOF

cat > "$TRACK_DIR/operations/default.json" <<'EOF'
{
  "name": "knn-search-10-100",
  "operation-type": "search",
  "param-source": "knn-param-source",
  "k": 10,
  "num-candidates": 100
},
{
  "name": "knn-search-100-1000",
  "operation-type": "search",
  "param-source": "knn-param-source",
  "k": 100,
  "num-candidates": 1000
}
EOF

cat > "$TRACK_DIR/challenges/default.json" <<'EOF'
{
  "name": "write-only",
  "default": true,
  "description": "Index 1M 1024-dim vectors, force-merge to 1 segment per shard",
  "schedule": [
    { "operation": { "operation-type": "delete-index" } },
    { "operation": { "operation-type": "create-index", "settings": {{index_settings | default({}) | tojson}} } },
    { "operation": { "operation-type": "cluster-health", "request-params": { "wait_for_status": "green" }, "retry-until-success": true } },
    {
      "name": "index-append",
      "operation": { "operation-type": "bulk", "bulk-size": {{bulk_size | default(500)}}, "ingest-percentage": {{ingest_percentage | default(100)}} },
      "warmup-time-period": {{bulk_warmup | default(40)}},
      "clients": {{bulk_indexing_clients | default(8)}}
    },
    { "name": "refresh-after-index", "operation": { "operation-type": "refresh", "request-timeout": 1200, "include-in-reporting": true } },
    {
      "name": "force-merge",
      "operation": { "operation-type": "force-merge", "mode": "polling", "max-num-segments": {{max_num_segments | default(1)}}, "request-timeout": 7200, "include-in-reporting": true }
    },
    { "name": "refresh-after-force-merge", "operation": "refresh" },
    {
      "name": "wait-until-merges-finish",
      "operation": { "operation-type": "index-stats", "index": "_all", "condition": { "path": "_all.total.merges.current", "expected-value": 0 }, "retry-until-success": true, "include-in-reporting": false }
    }
  ]
},
{
  "name": "query-only",
  "description": "ANN queries against the pre-built index (no indexing)",
  "schedule": [
    { "name": "knn-search-10-100", "operation": "knn-search-10-100", "warmup-iterations": 100, "iterations": 1000 },
    { "name": "knn-search-100-1000", "operation": "knn-search-100-1000", "warmup-iterations": 100, "iterations": 1000 }
  ]
}
EOF

# 与 s3 赛道同一 param-source/runner 骨架，仅 knn 字段名不同（emb vs vector）
cat > "$TRACK_DIR/track.py" <<'EOF'
import json
import os


class KnnParamSource:
    def __init__(self, track, params, **kwargs):
        # choose a suitable index: if there is only one defined for this track
        # choose that one, but let the user always override index
        if len(track.indices) == 1:
            default_index = track.indices[0].name
        else:
            default_index = "_all"

        self._index_name = params.get("index", default_index)
        self._cache = params.get("cache", False)
        self._exact_scan = params.get("exact", False)
        self._params = params
        self._queries = []

        cwd = os.path.dirname(__file__)
        with open(os.path.join(cwd, "queries.json"), "r") as file:
            for line in file:
                self._queries.append(json.loads(line))
        self._iters = 0
        self.infinite = True

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        result = {"index": self._index_name, "cache": self._params.get("cache", False), "size": self._params.get("k", 10)}

        if self._exact_scan:
            result["body"] = {
                "query": {
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query, 'emb') + 1.0",
                            "params": {"query": self._queries[self._iters]},
                        },
                    }
                },
                "_source": False,
            }
        else:
            result["body"] = {
                "knn": {
                    "field": "emb",
                    "query_vector": self._queries[self._iters],
                    "k": self._params.get("k", 10),
                    "num_candidates": self._params.get("num-candidates", 100),
                },
                "_source": False,
            }
        self._iters += 1
        if self._iters >= len(self._queries):
            self._iters = 0
        return result


def register(registry):
    registry.register_param_source("knn-param-source", KnnParamSource)
EOF

log "生成 queries.json（官方 queries.json.bz2 前 $NQUERIES 条）..."
bzcat "$QUERIES_BZ2" | head -n "$NQUERIES" > "$TRACK_DIR/queries.json"

# ---- 自检：维度 / 条数 / 文件齐备 --------------------------------------------
q1_dims=$(python3 -c "import json;print(len(json.loads(open('$TRACK_DIR/queries.json').readline())))" 2>/dev/null || echo 0)
[ "$q1_dims" = "1024" ] || die "查询向量维度异常: $q1_dims ≠ 1024"
qcount=$(wc -l < "$TRACK_DIR/queries.json")
c1_dims=$(head -n 1 "$CORPUS" | python3 -c "import json,sys;print(len(json.loads(sys.stdin.readline())['emb']))" 2>/dev/null || echo 0)
[ "$c1_dims" = "1024" ] || die "语料向量维度异常: $c1_dims ≠ 1024"
for f in track.json index.json track.py queries.json operations/default.json challenges/default.json documents-1024.json; do
  [ -s "$TRACK_DIR/$f" ] || die "赛道文件缺失: $f"
done

log "hv1024 赛道已生成: $TRACK_DIR（语料 $CORPUS_COUNT docs，查询 $qcount 条 × ${q1_dims} 维，3 分片）"
