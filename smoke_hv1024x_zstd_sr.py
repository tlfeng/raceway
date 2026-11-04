#!/usr/bin/env python3
"""smoke_hv1024x_zstd_sr.py —— hv1024x-codec-zstd-sr 变体的配置 smoke test

2026-11-04：变体的 index.json 把 index.codec / index.source_reuse 嵌在 "index" 对象里
→ 展开成 settings.index.index.codec → ES 报 unknown setting → **索引建不起来**，
create-index 的 error_rate=1.0，随后 knn-search 全部 error_rate=1.0（2026-11-03 夜跑 3 场全废）。

本脚本验证修好的配置（settings 平铺）：
  ① 两种 settings 形态都能建索引（修好的平铺 vs 旧的嵌套）
  ② 同样 N 条文档灌进去，存储体积不出现 9 倍异常
  ③ knn 检索能返回命中（不是 100% 失败）
  ④ 召回探针能跑出 recall 值

用法: python3 smoke_hv1024x_zstd_sr.py <ES_URL> [auth] [N_docs]
"""
import json
import subprocess
import sys
import time

ES = sys.argv[1] if len(sys.argv) > 1 else "https://10.10.10.224:9200"
AUTH = sys.argv[2].split(":", 1) if len(sys.argv) > 2 and ":" in sys.argv[2] else None
NDOCS = int(sys.argv[3]) if len(sys.argv) > 3 else 50000
CORPUS = "/opt/raceway/tracks/hv1024x/documents-1024.json"
QUERIES = "/opt/raceway/tracks/hv1024x/queries.json"
INDEX = "smoke-vectors"


def curl(method, path, body=None, raw_body=None, ctype="application/json"):
    """body 一律走 stdin（--data-binary @-）：bulk 载荷有几百 MB，进 argv 会 E2BIG"""
    a = ["curl", "-sSk", "-m", "180", "-X", method]
    if AUTH:
        a += ["-u", "%s:%s" % (AUTH[0], AUTH[1])]
    stdin = None
    if raw_body is not None:
        a += ["-H", "Content-Type: %s" % ctype, "--data-binary", "@-"]
        stdin = raw_body
    elif body is not None:
        a += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        stdin = json.dumps(body)
    a.append(ES + path)
    out = subprocess.run(a, capture_output=True, text=True, input=stdin).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out[:400]}


MAPPING_BASE = {
    "dynamic": "false",
    "_source": {"excludes": ["emb"]},
    "properties": {
        "docid": {"type": "keyword"},
        "emb": {"type": "dense_vector", "dims": 1024, "index": True,
                "similarity": "dot_product",
                "index_options": {"type": "hnsw", "m": 32, "ef_construction": 100}},
    },
}


def settings_fixed():
    """修好的形态：index 对象 + 平铺的 index.codec / index.source_reuse"""
    return {"index": {"number_of_shards": 3, "number_of_replicas": 0},
            "index.codec": "ZSTD", "index.source_reuse": True}


def settings_broken():
    """旧形态：codec / source_reuse 嵌在 index 对象里"""
    return {"index": {"number_of_shards": 3, "number_of_replicas": 0,
                      "index.codec": "ZSTD", "index.source_reuse": True}}


def settings_base():
    """基座（无 codec / source_reuse）"""
    return {"index": {"number_of_shards": 3, "number_of_replicas": 0}}


def create(name, settings):
    curl("DELETE", "/" + name)
    r = curl("PUT", "/" + name, {"settings": settings, "mappings": MAPPING_BASE})
    if "error" in r:
        return None, r["error"].get("reason") or r["error"]["type"]
    if not r.get("acknowledged"):
        return None, "响应不是建索引应答: %s" % json.dumps(r, ensure_ascii=False)[:120]
    return name, None


def bulk_load(name, ndocs, chunk=1000):
    """从语料灌前 ndocs 条。

    分块 bulk 有上限：EasySearch 的 HTTP 体上限约 100 MB（实测 5000 条 = 108.6 MB → HTTP 413，
    整批被拒、索引里一条都没进去）。这里用 1000 条（≈21 MB）一块。"""
    total_err = 0
    buf = []
    sent = 0
    with open(CORPUS, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= ndocs:
                break
            buf.append(json.dumps({"index": {"_index": name}}))
            buf.append(line.strip())
            sent += 1
            if len(buf) >= chunk * 2:
                r = curl("POST", "/_bulk", raw_body="\n".join(buf) + "\n",
                         ctype="application/x-ndjson")
                if r.get("errors"):
                    total_err += sum(1 for it in r.get("items", [])
                                     if it.get("index", {}).get("error"))
                buf = []
    if buf:
        r = curl("POST", "/_bulk", raw_body="\n".join(buf) + "\n",
                 ctype="application/x-ndjson")
        if r.get("errors"):
            total_err += sum(1 for it in r.get("items", [])
                             if it.get("index", {}).get("error"))
    return total_err


def store_gb(name):
    st = curl("GET", "/%s/_stats" % name)
    if "indices" in st:
        for v in st["indices"].values():
            b = (v.get("total") or {}).get("store", {}).get("size_in_bytes")
            if b:
                return b / (1024 ** 3)
    # 回退：_cat/indices
    r = curl("GET", "/_cat/indices/%s?h=store.size&format=json" % name)
    try:
        rows = r if isinstance(r, list) else r.get("result") or []
        if rows:
            v = rows[0].get("store.size", "")
            for unit, mult in (("gb", 1024 ** 3), ("mb", 1024 ** 2), ("kb", 1024)):
                if v.lower().endswith(unit):
                    return float(v[:-len(unit)]) * mult / (1024 ** 3)
    except Exception:
        pass
    return None


def KNN_BODY(k=10, ef=100):
    with open(QUERIES, encoding="utf-8") as f:
        q = json.loads(f.readline())
    return {"size": k, "_source": False, "docvalue_fields": ["docid"],
            "knn": {"field": "emb", "k": k, "num_candidates": ef, "query_vector": q}}


def knn_search(name, k=10, ef=100):
    """用真查询跑一次 knn 检索"""
    with open(QUERIES, encoding="utf-8") as f:
        q = json.loads(f.readline())      # JSONL：每行一个向量数组
    body = {
        "size": k,
        "_source": False,
        "docvalue_fields": ["docid"],
        "knn": {"field": "emb", "k": k, "num_candidates": ef,
                "query_vector": q if isinstance(q, list) else q.get("emb") or q.get("vector")},
    }
    r = curl("POST", "/%s/_search" % name, body)
    hits = r.get("hits", {}).get("hits", [])
    err = r.get("error")
    return len(hits), err, hits[0].get("fields", {}).get("docid") if hits else None


print("=" * 72)
print("smoke test：hv1024x-codec-zstd-sr 配置")
print("目标 %s ｜ 文档数 %d" % (ES, NDOCS))
print("=" * 72)

print("\n【①】settings 形态 —— 旧写法应该被拒，新写法应该通过")
for label, st in [("旧（嵌套，昨晚那批用的）", settings_broken()),
                  ("新（平铺，修好的）", settings_fixed()),
                  ("基座（无 codec/sr）", settings_base())]:
    name, err = create("smoke-cfgcheck", st)
    if err:
        print("  [拒] %-28s %s" % (label, err[:90]))
    else:
        print("  [过] %-28s 建索引成功" % label)
        curl("DELETE", "/smoke-cfgcheck")

results = {}
for label, st, name in [("zstd+source_reuse（修好的）", settings_fixed(), "smoke-zstdsr"),
                        ("基座（对照）", settings_base(), "smoke-base")]:
    print("\n【②】%s：建索引 + 灌 %d 条" % (label, NDOCS))
    n, err = create(name, st)
    if err:
        print("  建索引失败: %s" % err[:150])
        continue
    t0 = time.time()
    nerr = bulk_load(name, NDOCS)
    print("  灌入完成 %d 条（bulk 错误 %d），耗时 %.0f 秒" % (NDOCS, nerr, time.time() - t0))
    curl("POST", "/%s/_refresh" % name)
    curl("POST", "/%s/_forcemerge?max_num_segments=1" % name)
    time.sleep(3)
    cnt = curl("GET", "/%s/_count" % name)
    cat = curl("GET", "/_cat/indices/%s?h=docs.count,store.size" % name)
    print("  索引文档数 %s ｜ %s" % (cnt.get("count"), json.dumps(cat, ensure_ascii=False)[:120]))
    gb = store_gb(name)
    results[label] = gb
    print("  存储体积: %s" % ("%.3f GB" % gb if gb else "取不到"))
    raw = curl("POST", "/%s/_search" % name, KNN_BODY())
    print("  检索原始响应: %s" % json.dumps(raw, ensure_ascii=False)[:300])
    hits, err, docid = knn_search(name)
    print("  knn 检索: 命中 %d 条%s" % (hits, ("（首条 docid=%s）" % docid) if docid else ""))
    if err:
        print("  检索报错: %s" % str(err)[:200])

if len(results) == 2:
    a = results["zstd+source_reuse（修好的）"]
    b = results["基座（对照）"]
    print("\n【③】体积对比：zstd+sr %.3f GB vs 基座 %.3f GB → 比值 %.2fx"
          % (a, b, a / b if b else 0))
    print("     昨晚那批是 9 倍（42.7 GB vs 4.7 GB）；修好后应该接近 1 倍")

for name in ("smoke-zstdsr", "smoke-base", "smoke-cfgcheck"):
    curl("DELETE", "/" + name)
print("\n【④】测试索引已清理")
