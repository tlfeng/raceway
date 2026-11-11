#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench-export.py —— 把 rally race store + campaign 产物导出成 ES 入库载荷（离线，不需要 ES 集群）

用法（在压测机上）：
  python3 bench-export.py --out exports/<tag> \
      [--races ~/.rally/benchmarks/races] [--campaigns campaigns]

产出（对齐《20261016-benchmark结果入库ES方案》§3）：
  templates.json      benchmark-result-env / benchmark-result-race / benchmark-result-metric 三张索引的 index template（strict + snake_case）
  benchmark-result-env.jsonl     一环境一行（host/vm 台账，来自 sidecar）
  benchmark-result-race.jsonl     一场 race 一行（含实测 JDK/heap/核数、valid、口径B 速率、fingerprint）
  benchmark-result-metric.jsonl  race × task × metric × stat 一行（单位归一 + family + direction）
  manifest.json       计数与来源，供核对
  bench-apply.py      一键 bulk 导入（幂等 upsert）

时间一律 ms→s、字节→GB、吞吐保留 docs/s 与 ops/s。
error_rate 例外：rally 的 op_metrics.error_rate 本就是 0~1 的比例（实测见过 1.0 = 全量失败），
**不再按 % 折算**；折算会把 100% 打成 1%，掩盖掉 nested 查询全挂的事故场次。
"""
import argparse
import datetime
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import importlib

# 压缩 / source_reuse 两个维度的推导与 legacy 拆分放在 bench-history（与编排台
# 同一份实现），避免两处各写一套慢慢漂移。
H = importlib.import_module('bench-history')  # 文件名带连字符

DOCS = {"track": 11396503, "hnsw2m-2m-s3": 2000000, "nested-s3": 11203029,
        "hv1024-s3": 1000000, "dsj512-s3": 1000000, "dsj768-s3": 1000000,
        "dsj1024-s3": 1000000, "dsj1536-s3": 1000000, "dsj2048-s3": 1000000,
        "dsg960-s3": 1000000, "dsg1920-s3": 1000000,
        # geonames 存储压缩 × source_reuse 正交六变体（走 --track-params，不另建目录；
        # 语料与基座同一份 → doc 数相同）
        "geo-codec-def": 11396503, "geo-codec-def-sr": 11396503,
        "geo-codec-zstd": 11396503, "geo-codec-zstd-sr": 11396503,
        "geo-codec-bc": 11396503, "geo-codec-bc-sr": 11396503,
        # ds-* 的 rally track 字段是同名形式（实测 track_raw）
        "ds-j512": 1000000, "ds-j768": 1000000, "ds-j1024": 1000000,
        "ds-j1536": 1000000, "ds-j2048": 1000000, "ds-g960": 1000000,
        "ds-g1920": 1000000,
        # hv1024x 家族（含 OpenSearch 三臂与 bbqdisk）与 upsert 赛道
        "hv1024x": 1000000, "hv1024x-int8": 1000000, "hv1024x-bbq": 1000000,
        "hv1024x-bbqdisk": 1000000,
        "hv1024x-os": 1000000, "hv1024x-os-bbq": 1000000, "hv1024x-os-disk": 1000000,
        "geonames-upsert": 11396503}
# 语料名登记表（rally track 字段原值 → corpus_name），与 DOCS 并排用于 sidecar 缺席时兜底。
# track ↔ corpus 严格 1:1（rally track 定义自带 corpora）；hvtp200k 一次性批次不登记不臆造。
CORPUS_NAME = {"track": "geonames", "nested": "nested", "nested-s3": "nested",
               "hnsw2m-2m-s3": "dense-vector", "hv1024-s3": "dense-vector-1024",
               "geo-codec-def": "geonames", "geo-codec-def-sr": "geonames",
               "geo-codec-zstd": "geonames", "geo-codec-zstd-sr": "geonames",
               "geo-codec-bc": "geonames", "geo-codec-bc-sr": "geonames",
               "ds-j512": "dense-vector-512", "ds-j768": "dense-vector-768",
               "ds-j1024": "dense-vector-1024", "ds-j1536": "dense-vector-1536",
               "ds-j2048": "dense-vector-2048", "ds-g960": "dense-vector-960",
               "ds-g1920": "dense-vector-1920",
               "dsj512-s3": "dense-vector-512", "dsj768-s3": "dense-vector-768",
               "dsj1024-s3": "dense-vector-1024", "dsj1536-s3": "dense-vector-1536",
               "dsj2048-s3": "dense-vector-2048", "dsg960-s3": "dense-vector-960",
               "dsg1920-s3": "dense-vector-1920",
               "hv1024x": "dense-vector-1024", "hv1024x-int8": "dense-vector-1024",
               "hv1024x-bbq": "dense-vector-1024", "hv1024x-bbqdisk": "dense-vector-1024",
               "hv1024x-os": "dense-vector-1024", "hv1024x-os-bbq": "dense-vector-1024",
               "hv1024x-os-disk": "dense-vector-1024",
               "geonames-upsert": "geonames"}
TRACK_NAME = {"track": "geo", "hnsw2m-2m-s3": "hnsw", "nested-s3": "nested", "hv1024-s3": "hv1024"}


def track_name_of(raw):
    """变体目录名（track_raw）→ 基础赛道名。

    显式表（基座目录名）+ 规则（任意 `<base>-codec-*` 变体 → 它的 base）。
    规则化才能推广到全部基础赛道，新增赛道不用再改表。
    """
    if raw in TRACK_NAME:
        return TRACK_NAME[raw]
    if raw and '-codec-' in raw:
        return H.base_of(raw)
    return raw
# 变体目录 → (压缩, source_reuse) 两个**独立维度**的期望值：**按规则推导**
# （变体 id 本身就编码了压缩与 sr），不用查表 —— 新增赛道/变体不用改表。
# 压缩：default(LZ4) / ZSTD / best_compression(DEFLATE)
# source_reuse：true / false —— Easysearch 专有（ES 不认 index.source_reuse）
PHASE = {"write-only": "w", "query-only": "q", "index-only": "w",
         "full": "wq", "nested-search-challenge": "wq", "default": "wq"}
LOWER_BETTER = ("latency", "service_time", "processing_time", "error_rate", "duration",
                "indexing_time", "merge_time", "refresh_time", "flush_time", "gc",
                "disk", "store", "system", "memory", "queue_time")
# 注意：recall 是 higher-better（召回越高越好），不能进这个列表。
# 只列当前真正会产出且方向一致的 family —— 列表里出现"还没实现"的名字，
# 将来谁走 direction_of() 就会拿到相反的方向。
# ---------------------------------------------------------------------------
# 口径契约（词表）：stat / unit 只允许出现在这两个闭集里。
#
# 闭集的三个理由：
#  1) rally 的原始 stat 名有 "50_0"/"90_0"/"99_0"/"99_9"/"99_99"/"100_0"/"median"…
#     漏一个就原样漏进 stat 列，于是同一类分位出现两个名字，画图按 stat 过滤会静默漏一半。
#  2) rally 的 "median"（吞吐块）与 "50_0"（时延块）是同一个统计量，
#     却落成 p50 与 median 两个名字 —— 同一根 stat 轴上混两种标签，不可比。
#  3) unit 同理：漏一个就多一种量纲，跨 family 求和/对比会出错。
# 归一后 stat 轴全域一致；旧名 → 新名会改 _id（见 --drop 说明），必须重建索引。
CANON_STATS = ("value", "mean", "p50", "p90", "p99", "p999", "p9999", "p100", "min", "max")
CANON_UNITS = ("s", "ms", "ops/s", "docs/s", "pages/s", "MB/s", "GB", "MB", "bytes",
               "count", "ratio", "percent", "text", "")
CANON_QUALITY = ("ok", "degraded", "unknown")
# challenge 是**源值**封闭词表（runner 的 case "$ph" in w) write-only;; q) query-only;; *) full;;），
# 合并成一行时是多值数组 —— 值仍然是源值，不新增词表。
CANON_CHALLENGE = ("write-only", "query-only", "index-only", "full", "nested-search-challenge")
# codec 归一后的封闭词表
CANON_CODEC = ("default", "zstd", "best_compression")
# 规范赛道名封闭词表（19 个）。归一后命中不了登记表的场次**不写库**：
# 归一不出来说明这一场的赛道身份不可信（实测：fid 形态脏、user_tags.track 是解析器
# 写坏的字面量），留着它只会把看板的分线带偏 —— 挡住比带偏强。
#
# 词表必须与编排台的 BASE_TRACKS（bench-compose）一致 —— 编排台能排的赛道，
# 库里就得能查。两边对不上的后果是静默的：编排台排了、库里查不到。
# （BASE_TRACKS 全集是本词表的子集：hvtp200k-2s/3s 只有历史数据，runner 跑不了，
#   编排台不排 —— 见 bench-compose BASE_TRACKS 的注释。）
#
# hv1024x 家族的基座 —— 1M 文档 × 1024 维 float，dot_product，HNSW m=32 / efC=100，
# 3 分片。家族内的量化档（int8 / bbq）与压缩档（zstd + source_reuse）由
# knn_index_type / codec / source_reuse 三列承载，不另起赛道名（§2.6 同一套处理变量
# 口径）。变体目录名 hv1024x-int8 / hv1024x-bbq 由 bench-history.base_of 剥掉量化
# 后缀归到本基座。
# hv1024x-os / hv1024x-os-bbq / hv1024x-os-disk（OpenSearch 三臂）与
# hv1024x-bbqdisk（ES bbq_disk）是**独立目录定义**（mappings 不同）—— 各自是
# 规范赛道名，后缀不剥（bench-history.base_of 的 DIR_FAMILY_BASES 守这条）。
CANON_TRACK = ("geonames", "hnsw", "nested", "hv1024", "hv1024x", "hv1024x-bbqdisk",
               "hv1024x-os", "hv1024x-os-bbq", "hv1024x-os-disk", "geonames-upsert",
               "hvtp200k-2s", "hvtp200k-3s",
               "ds-j512", "ds-j768", "ds-j1024", "ds-j1536", "ds-j2048",
               "ds-g960", "ds-g1920")
CANON_LATENCY_MODE = ("open_loop", "closed_loop", "unknown")
# ---------------------------------------------------------------------------
# benchmark-result-metric 的字段白名单（方案 §1 修订「物化只给看板真正查得到的字段」+ §3.3）
#
# 指标行只保留三类东西：
#   ① 指标事实：family / metric / stat / value / unit / 原值备查 / direction；
#   ② 口径契约：trusted / latency_mode / backlog_ratio（面板闸门要用）；
#   ③ 看板真正用得到的维度：track（按赛道拆看板）、series_key（身份分线键）、
#      campaign（批次过滤）。
# run 维度（车代号 / engine / version / build / JDK / dims / segment_form / env_id /
# round / phase / codec / knn_params…）一律回 benchmark-result-race 查 —— 指标行上带一份只是重复：
# 既白付体积（每行 20+ 个身份字段，7.9 万行就是几十 MB），又多一份漂移面。
# 白名单是**导出期硬约束**：越界即判失败，脏载荷不进库（与 stat/unit 词表同级）。
#
# ⚠️ 白名单与 benchmark-result-metric 的 strict 模板必须成套存在：少了白名单，
# 导出器会退回「common 全量展开」，已入库的 21 字段契约被打回旧形状
# （体积 +30%，白名单护栏失效）→ ES 侧用 dynamic:strict 让越界行直接被拒收。
# ---------------------------------------------------------------------------
METRIC_FIELDS = frozenset((
    "_index", "_id",
    "race_id", "@timestamp", "task", "operation", "family", "metric", "stat",
    "value", "unit", "value_original", "unit_original", "value_text", "direction",
    # trusted：行级布尔「这一行的数字能不能用」。场次级闸门（非 VALID / host-noise）
    # 移到导出期 —— 不该入库的场次根本不写库，库里只剩行级可信度一个标记。
    "trusted", "latency_mode", "backlog_ratio",
    "track", "series_key", "campaign",
    # run_start：批次锚点（该批次最早一场 race 的时刻）。同一批次的所有行共享同一个值，
    # 按它分桶恒为一个桶 —— 批次跨 UTC 午夜时按 @timestamp 分桶会把一次实验切成两段。
    "run_start",
    # codec / source_reuse 是**处理变量**（这次怎么跑），不是身份。它们必须进 metric：
    # 看板按 series_key 分组，ES 不能 join，不带这两个字段就没法在指标行上排除
    # 存储变体（DESIGN §2.6）。
    "codec", "source_reuse",
    # phase：这行来自赛内哪个阶段（w/m/q）。它是源派生值（从 challenge 按固定表映射），
    # 词表一个字不改 —— 合并后同一场里有写段与查段两批行，靠它分开。
    # node：monitor 采样行专用（哪个虚机节点采的）。
    "phase", "node",
))
METRIC_REQUIRED = frozenset(("@timestamp", "family", "metric", "stat", "unit",
                             "direction", "track", "series_key"))
# ---------------------------------------------------------------------------
# benchmark-result-race / benchmark-result-env 字段白名单（存储方案 §2.2/§2.3 定稿）
#
# 与 metric 同一机制：
#   ① 白名单 = 导出期硬约束，越界即判失败；
#   ② 模板逐字段显式声明 + dynamic:strict，ES 侧越界直接拒收（双重拦截）；
#   ③ 导出期自检「文档字段 ⊆ 模板声明」，漏声明一个入库就是整批拒收。
# 字段分四类（存储方案 §一）：
#   身份正交六列      engine / version / engine_build / jdk_vendor / jdk_version / series_key
#   声明证据列        jdk_want / jdk_measured / jdk_match_want / heap_declared / heap_measured / cores_declared / cores_tag…
#                     —— 配置列只存实际生效值，漂移检查由编排 heap_assert 起跑前承担；pkg* 保留（溯源）
#   run 维度          track / campaign / round / dims / codec / env_id…
#   派生便利列        write_task_duration_s / throughput_docs_per_s_whole（报表用，非看板聚合口径）
#                     store_size_gb_post_merge / segments_post_merge（赛末 forcemerge 后快照）
RACE_FIELDS = frozenset((
    "_index", "_id",
    # 主键与时间。race_id 照搬 rally 原生 race-id（uuid，取主段；同批重跑双跑
    # 字节一致幂等实测），另一段 race-id 记在 source。
    "race_id", "@timestamp", "run_start", "campaign",
    # 身份正交六列（唯一事实源在本索引；benchmark-result-metric 只带 series_key）
    "engine", "version", "engine_build", "jdk_vendor", "jdk_version", "series_key",
    "car_code",
    # 声明（JDK 想要什么）+ 配置身份（这台车配了什么）+ 实测校验（跑的时候实际是什么）
    # 配置列只存**实际生效值**（裸名，无 want/measured 双层）：JDK 靠节点实测读数，
    # heap/cores 靠编排断言背书的标签（heap_assert 起跑前校验，fail-fast 替代赛后对账）。
    "heap_mb", "cores",
    # 包指纹
    "pkg", "pkg_sha256",
    # run 维度。challenge 是**多值**：一次测量可以由多个 challenge 段组成
    # （老协议写赛+查赛分开跑 → ["write-only","query-only"]；新协议一次跑完 → ["full"]）。
    # 值全是源值，不新增词表。
    "track", "challenge", "round", "dims",
    "codec", "source_reuse", "env_id", "nnodes",
    "clients", "bulk_size", "shards", "replicas",
    # 向量参数（从 flattened 拆成标量）
    "knn_index_type", "knn_element_type", "knn_m", "knn_ef_construction",
    "knn_num_candidates",
    # 语料与环境（从 flattened 拆成标量）
    "corpus_docs", "corpus_name", "cluster_revision", "cluster_flavor",
    "rally_version",
    # 赛末快照（forcemerge 之后）+ 溯源
    "store_size_gb_post_merge", "segments_post_merge", "source",
    # 可信度：场次级汇总（任一行 false 则 false）。判断单行看 metric 上的 trusted。
    "trusted",
))
RACE_REQUIRED = frozenset(("race_id", "@timestamp", "campaign", "track", "series_key"))
ENV_FIELDS = frozenset((
    "_index", "_id",
    "env_id", "host", "host_type", "vcpu", "mem_gb", "cpu_model",
    # disk_type/disk_path 目前恒 null（sidecar 未采集）——列先占住，采集补上后无需改模板
    "disk_type", "disk_path",
    "os", "kernel", "topology", "vm_nodes", "notes", "source", "created_at", "active",
))
ENV_REQUIRED = frozenset(("env_id",))
# 兜底别名（同一块里归一后撞名时用）：非致命，但必须在 manifest 里可见
_STAT_ALIAS_RE = re.compile(r"^(?:value|mean|p50|p90|p99|p999|p9999|p100|min|max)_raw\d*$")
STAT_MAP = {"50_0": "p50", "median": "p50", "90_0": "p90", "99_0": "p99",
            "99_9": "p999", "99_99": "p9999", "100_0": "p100",
            "min": "min", "max": "max", "mean": "mean", "value": "value"}
# 归一后仍与同块其它 key 撞名时的兜底后缀（保证 _id 唯一，撞名本身会被断言抓到）
STAT_ALIAS_SUFFIX = "_raw"
RESULT_METRICS = {
    "total_time": ("indexing_time", "ms"), "merge_time": ("merge_time", "ms"),
    "merge_count": ("merge", "count"), "refresh_time": ("refresh_time", "ms"),
    "refresh_count": ("refresh", "count"), "flush_time": ("flush_time", "ms"),
    "flush_count": ("flush", "count"), "merge_throttle_time": ("merge_throttle_time", "ms"),
    "indexing_throttle_time": ("indexing_throttle_time", "ms"),
    "young_gc_time": ("gc", "ms"), "young_gc_count": ("gc", "count"),
    "old_gc_time": ("gc", "ms"), "old_gc_count": ("gc", "count"),
    "store_size": ("disk", "bytes"), "translog_size": ("disk", "bytes"),
    "dataset_size": ("disk", "bytes"), "segment_count": ("disk", "count"),
    # memory_segments/doc_values/terms/norms/points/stored_fields 六项不入库：
    # ES 8.19 与 ez240/250 实测恒为 0（新 Lucene 把段 reader 挪到 mmap 堆外，
    # on-heap 估算衰减为 0；HNSW 向量图本就不在统计内）。仍在真实计数的 on-heap
    # 段内存（index_writer/version_map/fixed_bit_set）由 bench-sidecar 赛末补采。
}


def norm_value(v, raw_unit):
    """→ (归一值, 归一单位)"""
    if v is None:
        return None, None
    if raw_unit in ("ms",):
        return v / 1000.0, "s"
    if raw_unit == "bytes":
        return v / 1e9, "GB"
    if raw_unit == "%":
        return v / 100.0, "ratio"
    return v, raw_unit or ""


def direction_of(family):
    return "lower-better" if any(k in family for k in LOWER_BETTER) else "higher-better"


# system 家族（load-monitor.sh 采样）里各指标的量纲与方向。
# 百分比类一律归一到 0~1 的 ratio；吞吐类保留原单位。
_SYS_PCT = ("cpu_steal", "foreign_cpu", "cpu_usr", "cpu_sys", "bench_cpu")
_SYS_MBPS = ("disk_rw_mbs", "net_rx_mbs", "net_tx_mbs")
_SYS_GB = ("mem_avail_gb",)


def unit_of(metric):
    if metric.startswith(_SYS_PCT):
        return "ratio"
    if metric.startswith(_SYS_MBPS):
        return "MB/s"
    if metric.startswith(_SYS_GB):
        return "GB"
    if metric == "swapin":
        return "count"
    return ""


def direction_of_system(metric):
    """system 家族的方向：
    污染类（steal/外来进程/swap）与 CPU 负载类 → 越低越干净（lower-better）；
    吞吐类（磁盘/网络）与内存余量 → 越高越好（higher-better）。"""
    if metric.startswith(_SYS_MBPS) or metric.startswith(_SYS_GB):
        return "higher-better"
    return "lower-better"


def codec_dims(d, side):
    """本场**实测生效**的存储设置 → (compression, source_reuse) 两个**独立维度**。

    为什么拆两个维度：压缩（index.codec）与 source_reuse 是两个独立变量，
    糅在一个串里（旧的 "ZSTD+source_reuse"）就没法归因"是谁带来的差异"。
    拆开后任意一维都能单独分组对比。

    优先级：sidecar 的 _settings 回读（最权威，是实测值）
          → user_tags（新格式 codec/source_reuse 分开；旧格式 "ZSTD+source_reuse" 拆开）
          → 赛道变体名兜底。
    非 codec 变体场次 → (None, None)。
    """
    st = ((side.get("run_params") or {}).get("index_settings_measured") or {}) if side else {}
    for _idx, s in (st.items() if isinstance(st, dict) else []):
        c = (s or {}).get("index.codec")
        sr = str((s or {}).get("index.source_reuse", "")).lower()
        if c:
            return c, ("true" if sr == "true" else "false")
    ut = d.get("user-tags") or {}
    c = ut.get("codec")
    if c:
        return H.split_codec(c, ut.get("source_reuse"))
    tr = d.get("track")
    return H.compression_of(tr), (('true' if H.source_reuse_of(tr) else 'false')
                                  if H.source_reuse_of(tr) is not None else None)


def codec_of(d, side):
    """兼容旧调用点：只取压缩维度（source_reuse 另有 source_reuse_of）。"""
    return codec_dims(d, side)[0]


def source_reuse_of(d, side):
    """本场是否开了 index.source_reuse（Easysearch 专有设置）。"""
    return codec_dims(d, side)[1]


def knn_params_of(side):
    """KNN 索引参数（k / ef_search / num_candidates / M / ef_construction / index type / 量化）。

    没有实测来源就返回 None —— 不臆造。KNN 赛道只比吞吐不看召回、不记索引参数，
    "快"是拿不同候选集换来的，比值没有意义。
    """
    p = ((side or {}).get("run_params") or {}).get("knn_params")
    return p if isinstance(p, dict) and p else None


def measured_index_settings(side):
    """索引级实测设置 → {shards, replicas}。

    sidecar 的 index_settings_measured 记的是**建好之后**索引的实际设置
    （index.number_of_shards / index.number_of_replicas），比编排声明的 shards
    权威。声明缺了（sidecar run_params.shards 为 null）就用实测值补上；
    声明已有的值不改（避免动到既有行的口径）。
    """
    out = {}
    st = ((side or {}).get("run_params") or {}).get("index_settings_measured") or {}
    for _idx, _st in st.items():
        if not isinstance(_st, dict):
            continue
        for key, col in (("index.number_of_shards", "shards"),
                         ("index.number_of_replicas", "replicas")):
            try:
                out[col] = int(_st.get(key))
            except (TypeError, ValueError):
                pass
        break
    return out


def fid_of(d):
    ut = d.get("user-tags") or {}
    # 改名兼容：改名前写的是 user-tags.arm，改名后写 user-tags.car。两者都读，
    # 老的 race.json（改名前跑的那批）照样能解析出车的身份。
    car = ut.get("car") or ut.get("arm") or ut.get("engine") or "?"
    tr = track_name_of(d.get("track"))
    return car, tr, ut


# ------------------------------------------------------------------ 图例标签派生
#
# 看板图例只显示车名（ez250 / jdk25@3015 / kona25@3015）时，读者看不出三件事：
# 引擎版本（ez240 与 ez250 都是 EasySearch，版本 2.4.0 / 2.5.0）、
# 引擎 build（3015 / 3009 / 2975，同版本不同 build 的结论不能混）、
# JDK 发行版（jdk25 车是 Oracle GraalVM 25，kona25 车是 Tencent Kona 25，
# 两者主版本号都是 25，车名区分得开但看不出是两种发行版）。
#
# 系列键口径：引擎名@版本号@build号@JDK名
#   ez@2.4.0@2975@graalvm21     ez@2.5.0@3015@graalvm25
#   ez@2.5.0@3015@kona25        es@8.19.23@—@openjdk27
# 口径代码在 identity.py（唯一来源），这里只做引用 —— 别在这里另写一份。
#
# 身份维度正交拆列：engine / version / engine_build / jdk_vendor / jdk_version /
# series_key。车代号只留在 user_tags.car。
# jdk_vendor 必须单列：GraalVM 25 与 Kona 25 的完整版本号都是 25.0.4，
# 只有厂商能区分，而 JDK 厂商对比正是本项目要回答的问题。

try:
    identity_mod = importlib.import_module('identity')
except ImportError:                                                    # 从别处 import 本文件时
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    identity_mod = importlib.import_module('identity')
series_key_of = identity_mod.series_key_of
jdk_short_of = identity_mod.jdk_short_of
jdk_vm_of = identity_mod.jdk_vm_of
jdk_vendor_of = identity_mod.jdk_vendor_of
jdk_major_of = identity_mod.jdk_major_of
jdk_version_of = identity_mod.jdk_version_of


# ---------------------------------------------------------------- 环境台账派生 (方案 §3.1)

def env_host_type(env_id, vm_nodes):
    """物理机形态：kvm-vm / bare-metal / docker —— 从 env_id 与节点清单推断，不猜"""
    eid = (env_id or "").lower()
    if "kvm" in eid or "vm" in eid:
        return "kvm-vm"
    if "docker" in eid or "container" in eid:
        return "docker"
    if vm_nodes:
        return "kvm-vm" if any(n.get("ip") for n in vm_nodes) else "bare-metal"
    return None


def env_vcpu(vm_nodes):
    """每节点 vCPU（取第一个节点）"""
    for n in (vm_nodes or []):
        v = n.get("nproc_inside") or n.get("vcpu_hypervisor")
        if v:
            return v
    return None


def env_mem_gb(vm_nodes):
    """每节点内存 GB（取第一个节点）"""
    for n in (vm_nodes or []):
        v = n.get("mem_gib_hypervisor") or n.get("mem_gb")
        if v:
            return v
    return None


def env_notes(e, host_type):
    """环境说明：由 env 数据派生，不写死资产编号/机房等内部信息"""
    nodes = e.get("vm_nodes") or []
    if not nodes:
        return "单机被测集群；rally 发压机与被测集群分离"
    unit = {"kvm-vm": "台 KVM 虚机", "docker": "个容器"}.get(host_type, "个节点")
    return "%d %s组成的被测集群；rally 发压机与被测集群分离" % (len(nodes), unit)


def env_os(vm_nodes):
    """从节点内核推断发行版：6.8.x-generic → Ubuntu 24.04"""
    for n in (vm_nodes or []):
        k = n.get("kernel") or ""
        if k.startswith("6.8."):
            return "Ubuntu 24.04"
        if k.startswith("6.5.") or k.startswith("5.15."):
            return "Ubuntu 22.04"
        if k:
            return k
    return None


def load_store(races_dir):
    for p in sorted(glob.glob(os.path.join(races_dir, "*", "race.json"))):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                d = json.load(f)
        except (ValueError, OSError):
            continue
        if d.get("user-tags"):
            d["_path"] = p
            yield d


# ------------------------------------------------------------------ 源头证据
#
# 引擎身份（版本 + build）与 JDK 身份都有**实测/记录级**的源头，
# 比"从车代号猜"可靠得多。三条源头，按可信度从高到低：
#
#   ① sidecar（赛末实测）：engine.pkg 包名 + jdk.per_node 的 vendor/major
#   ② 部署日志（部署脚本自己写的）：logs/ibs-deploy-<车>.log 里的
#      "Deploying Easysearch 2.4.0-2975" / "Deploying Elasticsearch 8.19.21"
#      logs/jdk-<车>.txt 里的 java -version 原文
#   ③ revision 指纹（race.json 的 cluster.revision）：引擎源码提交号，
#      与包名一一对应（见 REVISION_IDENTITY，由①②实测归纳）
#
# 为什么不用 campaign 的 PACKAGES.txt：那是"这一批装了哪些包"的清单，
# 不是"这个车实际跑的是哪个包" —— 按它推会把车标成自相矛盾的组合
# （引擎版本还是 2.4.0，build 却是该批默认包的 3015）。
#
# revision → 身份对照表（每条都有部署日志或 sidecar 实测背书）：
#   d8d3be54… → EasySearch 2.5.0-3015（部署日志 ×6 + sidecar 实测）
#   fab83e9f… → EasySearch 2.5.0-3009（部署日志 ×3 + sidecar 实测）
#   79c5d197… → EasySearch 2.4.0-2975（部署日志 ×5 + sidecar 实测）
#   bf8501f2… → EasySearch 2.5.0-3003（部署日志 ×2）
#   b21f3974… → EasySearch 2.5.0（构建号无记录 —— 这批只有 car-es819.log，
#                没有部署日志；不猜）
#   4fe44c25… → Elasticsearch 8.19.21（部署日志 ×2）
#   6940e499… → Elasticsearch 8.19.23（部署日志 ×3 + sidecar 实测）
REVISION_IDENTITY = {
    "d8d3be542d807ad57d4becbd0d4e95b560107453": ("easysearch", "2.5.0", "3015"),
    "fab83e9f78f32c50dcd3793ff9e5a2d8c19210a9": ("easysearch", "2.5.0", "3009"),
    "79c5d197a9ba7b28ec89b149c9eb320c52c5a784": ("easysearch", "2.4.0", "2975"),
    "bf8501f2c621318bc031a859615402379ca9d7c1": ("easysearch", "2.5.0", "3003"),
    "b21f39745344f63923d66df18875f1ddf487c5c6": ("easysearch", "2.5.0", None),
    "4fe44c255c3d0da06779b921e132cdc555ed9aff": ("elasticsearch", "8.19.21", None),
    "6940e499f8a38841809b8e295a20c6421c95d9e4": ("elasticsearch", "8.19.23", None),
}

_DEPLOY_RE = re.compile(r"Deploying\s+(Easysearch|Elasticsearch)\s+([0-9][0-9.]*)(?:-(\d+))?")


def parse_deploy_logs(camp_dir):
    """campaign/logs 里的部署日志 → {车或引擎名: (engine, version, build)}。

    文件名两种写法都见过：ibs-deploy-<车>.log（ibs 部署脚本）与 deploy-<车>.log。
    车名取文件名（ez240 / jdk25 / es819…），也可能是引擎名（easysearch /
    elasticsearch）——那是"该批默认引擎"的部署记录，对该批所有对应引擎的车生效。
    """
    out = {}
    for p in glob.glob(os.path.join(camp_dir, "logs", "*deploy-*.log")):
        base = os.path.basename(p)
        mm = re.match(r"^(?:ibs-)?deploy-(.+)\.log$", base)
        if not mm:
            continue
        key = mm.group(1)
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = _DEPLOY_RE.search(txt)
        if not m:
            continue
        eng = "elasticsearch" if m.group(1) == "Elasticsearch" else "easysearch"
        out[key] = (eng, m.group(2), m.group(3))
    return out


def parse_jdk_files(camp_dir):
    """campaign/logs/jdk-<车>.txt 里的 java -version → {车: {vendor, major, version}}。

    没有 sidecar 的批次靠它拿 JDK 身份（实测原文，不是从车名猜）。
    version 是**完整**版本号（25.0.4 / 26.0.2.1 / 21.0.9），major 只取第一段。
    """
    out = {}
    for p in glob.glob(os.path.join(camp_dir, "logs", "jdk-*.txt")):
        car = os.path.basename(p)[len("jdk-"):-len(".txt")]
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        vendor = None
        if "TencentKonaJDK" in txt:
            vendor = "TencentKonaJDK"
        elif "GraalVM" in txt:
            vendor = "GraalVM"
        elif "OpenJDK" in txt or "openjdk" in txt:
            vendor = "OpenJDK"
        mm = re.search(r'(?:java version|openjdk version)\s+"([0-9][0-9.]*)', txt)
        version = mm.group(1) if mm else None
        major = version.split(".")[0] if version else None
        if vendor and major:
            out[car] = {"vendor": vendor, "major": major, "version": version}
    return out


def load_campaign_meta(camp_dir):
    """RACES.md 状态、PACKAGES 指纹、sidecar、monitors 摘要"""
    m = {"status": {}, "packages": "", "sidecar": {}, "sidecar_by_race": {},
         "post_merge": {}, "env": {}, "env_id": None}
    m["deploys"] = parse_deploy_logs(camp_dir)
    m["jdks"] = parse_jdk_files(camp_dir)
    rp = os.path.join(camp_dir, "RACES.md")
    if os.path.isfile(rp):
        for line in open(rp, encoding="utf-8", errors="replace"):
            c = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(c) >= 2 and re.match(r"^[a-z0-9]+-", c[0]):
                m["status"][c[0]] = c[1]
    pp = os.path.join(camp_dir, "PACKAGES.txt")
    if os.path.isfile(pp):
        m["packages"] = open(pp, encoding="utf-8", errors="replace").read()
    # 本 campaign 自证的引擎包名（从 PACKAGES.txt 的 sha256+包名行解析）：
    #   没有 sidecar 的 campaign 只能靠它，
    #   否则会套用别的 campaign 实测到的车身份，把 3009 的场次标成 3015。
    for _mm in re.finditer(r"(easysearch|elasticsearch)-[0-9][^\s]*\.tar\.gz", m.get("packages") or ""):
        m.setdefault("pkg_own", {}).setdefault(_mm.group(1), _mm.group(0))
    # 受污染场次清单（每行一个 fid）：给该场 benchmark-result-race 文档打 caliber_warnings 标注。
    # 为什么单独存文件而不是写进 RACES.md：RACES.md 的状态列直接决定 valid，
    # 写成 CONTAMINATED 会让 valid=false，看板闸门 valid:true 会把这一场整个滤掉
    # （有数据的场一滤就空面板）。状态保持 VALID，
    # 污染事实走这个清单 + 文档 caliber_warnings 双通道留痕。
    # 臂级 build 表（PROGRESS.txt / SUMMARY.md 的臂头行）——与有没有 CONTAMINATED.txt 无关
    m["arm_builds"] = load_arm_builds(camp_dir)
    cf = os.path.join(camp_dir, "CONTAMINATED.txt")
    if os.path.isfile(cf):
        m["contaminated"] = [x.strip() for x in open(cf, encoding="utf-8", errors="replace")
                             if x.strip() and not x.strip().startswith("#")]
    for p in glob.glob(os.path.join(camp_dir, "results", "*-sidecar.json")):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        m["sidecar"][s["run"]["fid"] + "-" + s["run"]["phase"]] = s
        # 连接键用 race_id（sidecar_by_race），不能用场次名拼：重试场的 fid 带 -tryN，
        # 而 race.json 里没有轮试号 —— 拼名会丢 sidecar（实测 JDK/heap 全 null）
        # 并套错 RACES.md 状态
        rid = (s.get("run") or {}).get("race_id")
        if rid:
            m["sidecar_by_race"][rid] = s
        m["env_id"] = s.get("env", {}).get("env_id") or m["env_id"]
        m["env"] = s.get("env") or m["env"]
    # 赛末（forcemerge 之后）落盘体积：节点统计原始探针，三节点 store 之和。
    # 拆分协议落档 <fid>-q、单挑战协议落档 <fid>-wq（merge 都发生在探针之前），两个都认。
    for p in glob.glob(os.path.join(camp_dir, "logs", "fp", "*-q", "nodes-stats.txt")) + \
             glob.glob(os.path.join(camp_dir, "logs", "fp", "*-wq", "nodes-stats.txt")):
        _fpdir = os.path.basename(os.path.dirname(p))
        fid = _fpdir[:-3] if _fpdir.endswith("-wq") else _fpdir[:-2]
        try:
            nd = json.load(open(p, encoding="utf-8")).get("nodes") or {}
        except (ValueError, OSError):
            continue
        if not nd:
            continue
        store = sum(((n.get("indices") or {}).get("store") or {}).get("size_in_bytes", 0) for n in nd.values())
        segs = sum(((n.get("indices") or {}).get("segments") or {}).get("count", 0) for n in nd.values())
        m["post_merge"][fid] = {"store_gb": round(store / 2**30, 4) if store else None,
                                "segments": segs, "nodes": len(nd)}
    for p in glob.glob(os.path.join(camp_dir, "monitors", "*-summary.txt")):
        base = os.path.basename(p)[:-len("-summary.txt")]
        txt = open(p, encoding="utf-8", errors="replace").read()
        g = lambda k: (float(re.search(k + r"=([0-9.]+)", txt).group(1))
                       if re.search(k + r"=([0-9.]+)", txt) else None)
        m.setdefault("mon", {})[base] = {
            "cpu_steal_avg": g("steal_avg"), "cpu_steal_max": g("steal_max"),
            "foreign_cpu_max": g("foreign_max"), "swapin": g("swapin"),
            # load-monitor.sh 每 5 秒就采到、但摘要里只吐 5 个键的其余量 ——
            # 这里一并接住（无需新增探针、无需改采样节奏）：
            "cpu_usr_avg": g("cpu_usr_avg"), "cpu_usr_max": g("cpu_usr_max"),
            "cpu_sys_avg": g("cpu_sys_avg"), "cpu_sys_max": g("cpu_sys_max"),
            "bench_cpu_avg": g("bench_cpu_avg"), "bench_cpu_max": g("bench_cpu_max"),
            "disk_rw_mbs_avg": g("disk_rw_mbs_avg"), "disk_rw_mbs_max": g("disk_rw_mbs_max"),
            "net_rx_mbs_avg": g("net_rx_mbs_avg"), "net_tx_mbs_avg": g("net_tx_mbs_avg"),
            "mem_avail_gb_min": g("mem_avail_gb_min"),
            "env_verdict": (re.search(r"ENV-VERDICT: (\S+)", txt).group(1)
                            if re.search(r"ENV-VERDICT: (\S+)", txt) else None)}
    return m


# 写入阶段的 degraded 阈值
#
# degraded 的动机：error_rate=1.0（该 task 全部请求失败）时，
# rally 吐出的时延量的是"失败响应有多快"（实测 nested p99 只有 0.22s），
# 混进延迟曲线会把引擎显得比实际更快 —— 所以这类行不能进图。
#
# 但"error_rate > 0"一刀切过严：GIST 系向量赛道（ds-g1920 / ds-g960）的
# index-append error_rate 稳定在 0.0017~0.0023（约 0.2%，100 万条里丢 2000 条），
# 多个批次都复现 —— 是**可复现的血统特性**，不是测量事故。这种量级的 bulk 偶发失败
# 不影响吞吐/时延的量级（rally 按成功处理量计吞吐），检索质量也没塌，
# 却会让这两个赛道的写入数据整批被 quality 闸门挡在看板外。
#
# 所以：写入阶段 error_rate > 1% 才降级；查询侧维持 > 0 即降级
# ——查询 task 全失败的时延污染才是主要风险，查询侧不放松。
WRITE_DEGRADED_ERROR_RATE = 0.01


def is_write_task(task):
    """写阶段 task（index-*）。与场次级 write_degraded 判定同口径。"""
    return str(task or "").startswith("index")


def task_quality(om, task=None):
    """单个 operation 的可信度。

    ok       = error_rate 在阈值以下，该 task 的时延/吞吐是成功请求的
    degraded = 超过阈值（写阶段 > 1%，查询阶段 > 0；
                rally 记的是 0~1 比例，1.0 = 该 task 全部请求失败）
    unknown  = rally 没给 error_rate
    """
    er = om.get("error_rate")
    if er is None:
        return "unknown"
    try:
        er = float(er)
    except (TypeError, ValueError):
        return "unknown"
    thr = WRITE_DEGRADED_ERROR_RATE if is_write_task(task) else 0.0
    return "degraded" if er > thr else "ok"


def _blk_mean(blk, stats=("mean", "p50", "median", "50_0")):
    """取块里的一个代表值（优先 mean，其次 p50/median/50_0 —— 原始块里是 rally 的
    "50_0" 这种 key，不是归一后的 "p50"）"""
    for s in stats:
        v = blk.get(s)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def latency_semantics(lat_blk, svc_blk, gt_mode):
    """判定一个 task 的 latency 口径。

    rally 的 latency 有两种语义，取决于该 operation 是否设了 target-throughput（开环节流）：
      * 闭环（无 target-throughput）：latency ≡ service_time，量的是单次请求耗时
      * 开环（有 target-throughput）：latency = 从计划发出到响应返回的端到端时间，
        **包含发压机侧的积压延迟**；service_time 才是请求本身的服务端耗时
    实测：nested 赛道 6 个 operation 全带 target-throughput，其中 5 个实际饱和
    （target 20 ops/s×2 clients，achieved 仅 19.86），90 分位 latency 1.477s 而
    service_time 只有 36.7ms —— 差的 40 倍全是发压机积压，不是引擎慢。

    返回 (mode, backlog_ratio, source)：
      mode   ∈ {"open_loop", "closed_loop", "unknown"}
      backlog_ratio = (latency_mean - service_time_mean) / latency_mean ∈ [0,1]
      source ∈ {"groundtruth", "inferred", "unknown"}
    gt_mode 来自 track spec 实参（sidecar.run_params.latency_modes），是地面真值；
    没有真值时按数据推断（推断不可证伪，单独计数上报，不当成实测）。
    """
    lm, sm = _blk_mean(lat_blk), _blk_mean(svc_blk)
    ratio = None
    if lm is not None and sm is not None and lm > 0:
        ratio = max(0.0, min(1.0, (lm - sm) / lm))
    if gt_mode in ("open_loop", "closed_loop"):
        return gt_mode, ratio, "groundtruth"
    if ratio is None:
        return "unknown", None, "unknown"
    return ("open_loop" if ratio > 0.01 else "closed_loop"), ratio, "inferred"


# ---------------------------------------------------------------------------
# 召回率（recall）探针结果
# ---------------------------------------------------------------------------
# 召回率是方案 §一 的硬护栏（Recall@10 ≥ 0.95 / Recall@100 ≥ 0.99），
# 但探针（hv-recall-self.py）是查询跑完之后离线跑的，结果落在
# campaigns/<campaign>/results/recall-<fid>[-k<k>].json ——
# 既不在 race.json 里，也没进 sidecar，导出器的 recall 分支（读 sidecar["recall"]）
# 一直是空的：全库 family 分布里查不到 recall，护栏形同虚设。
#
# 这里把它接进导出：campaign + fid 对上该场**查询** race，就随该场一起出指标行，
# 于是 recall 行与查询曲线同 race_id、同 series_key、同 track，能在面板里对齐。
#
# k → esrally task 名：探针的 num_candidates 就是赛道里 KNN 的 ef 值
# （k=10/ef=100 → knn-search-10-100，k=100/ef=1000 → knn-search-100-1000），
# 对齐后 recall 才能按 task 与吞吐/时延面板并排比。
RECALL_TASK_BY_K = {10: "knn-search-10-100", 100: "knn-search-100-1000"}


def load_recall_index(campaigns_dir):
    """扫 campaigns/*/results/recall-*.json → {(campaign, fid): {task: {stat: value}}}"""
    idx = {}
    for camp in sorted(glob.glob(os.path.join(campaigns_dir, "*"))):
        if not os.path.isdir(camp):
            continue
        camp_name = os.path.basename(camp)
        best = {}
        for p in sorted(glob.glob(os.path.join(camp, "results", "recall-*.json"))):
            # 同一个 (fid,k) 可能有两个文件：合并版 recall-<fid>.json 与拆分版
            # recall-<fid>-k<k>.json（探针跑过两遍，值略有差异）。拆分版粒度更细，优先取它。
            prio = 2 if re.search(r"-k\d+\.json$", os.path.basename(p)) else 1
            try:
                txt = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for line in txt.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                tag = d.get("tag") or ""
                k = d.get("k")
                avg = d.get("avg_recall")
                if k is None or avg is None:
                    continue
                # tag 形态有两种：合并版写 "<fid>-k<k>"，拆分版写 "<fid>"（k 在字段里）
                m = re.match(r"^(?P<fid>.+)-k(?P<k>\d+)$", tag)
                if m:
                    fid = m.group("fid")
                    k = int(m.group("k"))
                else:
                    fid = tag
                key = (fid, int(k))
                cur = best.get(key)
                if cur is None or prio >= cur[0]:
                    mn = d.get("min_recall")
                    best[key] = (prio, float(avg),
                                 (float(mn) if mn is not None else None))
        for (fid, k), (_prio, avg, mn) in best.items():
            task = RECALL_TASK_BY_K.get(k)
            if not task:
                continue
            slot = idx.setdefault((camp_name, fid), {}).setdefault(task, {})
            slot["mean"] = avg
            if mn is not None:
                slot["min"] = mn
    return idx


def emit_metrics(d, run_id, ts, common, out, side=None, quality_of=None, counters=None):
    """race.json → benchmark-result-metric 行"""
    res = d.get("results") or {}
    gt_modes = ((side or {}).get("run_params") or {}).get("latency_modes") or {}

    def _push(row):
        """指标行只留白名单字段（run 维度回 benchmark-result-race 查）—— 见 METRIC_FIELDS 注释"""
        out.append({k: v for k, v in row.items() if k in METRIC_FIELDS})

    for om in res.get("op_metrics") or []:
        task = om.get("task") or om.get("operation") or "?"
        q = quality_of(om) if quality_of is not None else task_quality(om, task)
        lat_blk = om.get("latency") or {}
        svc_blk = om.get("service_time") or {}
        mode, backlog, lsrc = latency_semantics(lat_blk, svc_blk, gt_modes.get(task))
        if counters is not None:
            counters["latency_mode_" + lsrc] = counters.get("latency_mode_" + lsrc, 0) + 1
        row_common = dict(common)
        # 行级 trusted：这一行的数字能不能用。degraded = 该 task 的 error_rate 超阈值
        # （写阶段 >1%、查询侧 >0），它吐出的时延量的是"失败响应有多快"，
        # 混进延迟曲线会把引擎显得比实际更快 → 这一行不可信。
        row_common["trusted"] = (q != "degraded")
        row_common["latency_mode"] = mode
        if backlog is not None:
            row_common["backlog_ratio"] = backlog
        for key in ("throughput", "latency", "service_time", "processing_time"):
            blk = om.get(key) or {}
            unit = blk.get("unit") or ({"throughput": "ops/s", "latency": "ms",
                                        "service_time": "ms", "processing_time": "ms"}[key])
            family = key
            # 撞名判定必须**按 family 独立**：_id 里带 family，latency 的 p50 与
            # service_time 的 p50 是两行数据，不算撞名。只有同一个块里既有 50_0
            # 又有 median 这种才需要兜底后缀。
            seen_stat = {}
            for k, v in blk.items():
                if k == "unit" or v is None:
                    continue
                stat = STAT_MAP.get(k, k)
                base, n = stat, 1
                while stat in seen_stat:
                    stat = "%s%s%d" % (base, STAT_ALIAS_SUFFIX, n)
                    n += 1
                seen_stat[stat] = True
                nv, nu = norm_value(v, unit)
                _push({"_index": "benchmark-result-metric", "_id": "%s|%s|%s|%s|%s" % (run_id, row_common.get("phase"), task, family, stat),
                            "@timestamp": ts, "task": task, "operation": om.get("operation"),
                            "family": family, "metric": family, "stat": stat,
                            "value": nv, "unit": nu, "value_original": v, "unit_original": unit,
                            "direction": direction_of(family), **row_common})
        # ---- 派生：发压机积压 = latency - service_time（开环 task 才会有）----
        lat_u, svc_u = lat_blk.get("unit"), svc_blk.get("unit")
        if lat_u and svc_u and lat_u == svc_u:
            qseen = {}
            for k in sorted(set(lat_blk) & set(svc_blk)):
                if k == "unit":
                    continue
                lv, sv = lat_blk.get(k), svc_blk.get(k)
                if lv is None or sv is None:
                    continue
                try:
                    diff = float(lv) - float(sv)
                except (TypeError, ValueError):
                    continue
                if diff <= 0:
                    continue
                stat = STAT_MAP.get(k, k)
                base, n = stat, 1
                while stat in qseen:
                    stat = "%s%s%d" % (base, STAT_ALIAS_SUFFIX, n)
                    n += 1
                qseen[stat] = True
                nv, nu = norm_value(diff, lat_u)
                _push({"_index": "benchmark-result-metric", "_id": "%s|%s|%s|queue_time|%s" % (run_id, row_common.get("phase"), task, stat),
                            "@timestamp": ts, "task": task, "operation": om.get("operation"),
                            "family": "queue_time", "metric": "queue_time", "stat": stat,
                            "value": nv, "unit": nu, "value_original": diff, "unit_original": lat_u,
                            "direction": direction_of("queue_time"), **row_common})
        for key in ("error_rate", "duration"):
            v = om.get(key)
            if v is None:
                continue
            fam = key
            # error_rate: rally 给的就是 0~1 比例（实测见过 1.0 = 该 task 全部请求失败），
            #   当成百分比再 /100 会把 100% 打成 1%，把事故场次洗成"几乎无错"。
            # duration: rally 给毫秒，归一到秒。
            raw_unit = "ratio" if key == "error_rate" else "ms"
            nv, nu = norm_value(v, raw_unit)
            _push({"_index": "benchmark-result-metric", "_id": "%s|%s|%s|%s|value" % (run_id, row_common.get("phase"), task, fam),
                        "@timestamp": ts, "task": task, "operation": om.get("operation"),
                        "family": fam, "metric": key, "stat": "value", "value": nv, "unit": nu,
                        "value_original": v, "unit_original": raw_unit,
                        "direction": direction_of(fam), **row_common})
    # ---- 赛级指标（results 块）----
    for key, (fam, unit) in RESULT_METRICS.items():
        v = res.get(key)
        if v is None:
            continue
        if isinstance(v, dict):          # *_per_shard：取 median
            v = v.get("median")
            if v is None:
                continue
            stat = "median_per_shard"
        else:
            stat = "value"
        nv, nu = norm_value(v, unit)
        _push({"_index": "benchmark-result-metric", "_id": "%s|%s||%s|%s" % (run_id, common.get("phase"), fam + "_" + key, stat),
                    "@timestamp": ts, "task": None, "operation": None,
                    "family": fam, "metric": key, "stat": stat, "value": nv, "unit": nu,
                    "value_original": v, "unit_original": unit,
                    "direction": direction_of(fam), **common})
    # ---- 召回率（ground truth 由 campaign 离线算好写进 sidecar）----
    for task, m in ((side or {}).get("recall") or {}).items():
        if not isinstance(m, dict):
            continue
        for label, v in m.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            stat = STAT_MAP.get(label, label)
            _push({"_index": "benchmark-result-metric", "_id": "%s|%s|recall|%s" % (run_id, task, stat),
                        "@timestamp": ts, "task": task, "operation": None,
                        "family": "recall", "metric": "recall", "stat": stat,
                        "value": fv, "unit": "ratio", "value_original": fv, "unit_original": "ratio",
                        "direction": "higher-better", **common})


# ---------------------------------------------------------------------------
# 导出期口径契约断言
#
# 导出是纯函数、产出可 diff 的载荷快照 —— 架构上完全有能力在写出前发现口径缺陷。
# 下面三类问题会全部静默进 ES，必须在导出期拦住：
#   1) stat 词表越界：rally 的 99_99 分位原样漏进 stat 列，与 p999 并存
#   2) 失败 task 的时延行与成功 task 混在同一张延迟曲线里
#   3) 同一 (family, stat) 在不同 task 下量着不同的东西（开环/闭环的 latency）
# 这三条是**致命**的：导出直接非零退出，不让脏数据进库。
# 覆盖率与一致性提示是**非致命**的：写进 manifest 供核对，不阻断流水线。
# ---------------------------------------------------------------------------
def validate_payload(runs, metrics, envs=()):
    """返回 (problems, warnings, coverage)"""
    problems, warnings = [], []
    ids = set()
    stat_count, unit_count, aliases = {}, {}, {}
    for m in metrics:
        i = m.get("_id")
        if i in ids:
            problems.append("重复 _id（后写的会静默覆盖先写的）: %s" % i)
        ids.add(i)
        st = m.get("stat")
        if st not in CANON_STATS:
            if st and _STAT_ALIAS_RE.match(st):
                aliases[st] = aliases.get(st, 0) + 1
            else:
                problems.append("stat 越界（不在词表内）: %r  %s" % (st, i))
        stat_count[st] = stat_count.get(st, 0) + 1
        u = m.get("unit")
        if u not in CANON_UNITS:
            problems.append("unit 越界（不在词表内）: %r  %s" % (u, i))
        unit_count[u] = unit_count.get(u, 0) + 1
        tv = m.get("trusted")
        if tv is not None and not isinstance(tv, bool):
            problems.append("trusted 必须是裸布尔（不是字符串/数字）: %r  %s" % (tv, i))
        cv = m.get("codec")
        if cv is not None and cv not in CANON_CODEC:
            problems.append("codec 越界（不在词表内）: %r  %s" % (cv, i))
        lm = m.get("latency_mode")
        if lm is not None and lm not in CANON_LATENCY_MODE:
            problems.append("latency_mode 越界（不在词表内）: %r  %s" % (lm, i))
    if aliases:
        warnings.append("stat 归一后撞名，已用兜底别名区分（%d 行）: %s"
                        % (sum(aliases.values()), sorted(aliases)))
    # ---- 字段白名单（方案 §1 修订）----
    # run 维度回流到指标行 = 精简前功亏一篑：体积白付 + 多一份漂移面。
    # 越界即判失败 —— 与 stat/unit 词表同级，都是"导出期就拦住脏载荷"。
    extra_hits, missing_hits = {}, {}
    for m in metrics:
        extra = set(m.keys()) - METRIC_FIELDS
        if extra:
            k = ",".join(sorted(extra))
            extra_hits[k] = extra_hits.get(k, 0) + 1
        miss = METRIC_REQUIRED - set(m.keys())
        if miss:
            k = ",".join(sorted(miss))
            missing_hits[k] = missing_hits.get(k, 0) + 1
    for k, v in sorted(extra_hits.items()):
        problems.append("benchmark-result-metric 行出现白名单外字段（run 维度回流?）: %s × %d 行" % (k, v))
    for k, v in sorted(missing_hits.items()):
        problems.append("benchmark-result-metric 行缺必填字段 %s: %d 行" % (k, v))
    # ---- race / env 白名单（D10：两张表与 metric 同机制，越界即失败）----
    for name, docs, fields, required in (
            ("benchmark-result-race", runs, RACE_FIELDS, RACE_REQUIRED),
            ("benchmark-result-env", list(envs), ENV_FIELDS, ENV_REQUIRED)):
        r_extra, r_miss = {}, {}
        for d in docs:
            e = set(d.keys()) - fields
            if e:
                k = ",".join(sorted(e))
                r_extra[k] = r_extra.get(k, 0) + 1
            m = required - set(d.keys())
            if m:
                k = ",".join(sorted(m))
                r_miss[k] = r_miss.get(k, 0) + 1
        for k, v in sorted(r_extra.items()):
            problems.append("%s 行出现白名单外字段: %s × %d 行" % (name, k, v))
        for k, v in sorted(r_miss.items()):
            problems.append("%s 行缺必填字段 %s: %d 行" % (name, k, v))
    # 同一 (场次, task) 的所有行 trusted 必须一致
    # 注意必须用复合键：task 名在不同场次之间会重名（index-append 每个场次都有），
    # 只按 task 分组会把不同场次的可信度标记混在一起比。
    tq = {}
    for m in metrics:
        t = m.get("task")
        if not t:
            continue
        k = (m.get("race_id"), t)
        q = m.get("trusted")
        if k in tq and tq[k] != q:
            problems.append("同一 (场次, task) 的行 trusted 不一致: %s/%s (%s vs %s)"
                            % (k[0], t, tq[k], q))
        tq.setdefault(k, q)
    # error_rate **超阈值**的 task 整块必须 degraded
    # （写阶段 1% 阈值、查询侧 > 0 —— 与 task_quality 同口径，见 WRITE_DEGRADED_ERROR_RATE）
    for m in metrics:
        if m.get("family") == "error_rate" and (m.get("value") or 0) > 0:
            k = (m.get("race_id"), m.get("task"))
            thr = WRITE_DEGRADED_ERROR_RATE if is_write_task(m.get("task")) else 0.0
            if (m.get("value") or 0) > thr and tq.get(k) is not False:
                problems.append("error_rate 超阈值的 (场次, task) 未标不可信: %s/%s" % k)
    # closed_loop 的 task 不应出现积压（口径与实测矛盾）
    for m in metrics:
        if m.get("family") == "latency" and m.get("latency_mode") == "closed_loop":
            br = m.get("backlog_ratio")
            if br is not None and br > 0.01:
                problems.append("closed_loop 任务出现积压（口径矛盾）: %s br=%.3f"
                                % (m.get("task"), br))
    # 场次级：trusted 必须是各 task 的最差（任一 task 不可信 → 整场不可信）
    race_worst = {}
    for (rid, _t), tv in tq.items():
        race_worst[rid] = race_worst.get(rid, True) and (tv is not False)
    for r in runs:
        rid = r.get("race_id")
        worst = race_worst.get(rid)
        if worst is None:
            continue
        if r.get("trusted") is True and not worst:
            problems.append("场次标可信但有 task 不可信: %s" % rid)
    # challenge 是源值封闭词表（老协议写赛+查赛合并成一行时是多值）
    for r in runs:
        for cv in (r.get("challenge") or []):
            if cv not in CANON_CHALLENGE:
                problems.append("challenge 越界（不在源值词表内）: %r  %s" % (cv, r.get("race_id")))
    # ---- 指标行必须归属已入库的场次 ----
    # 每行 metric 的 race_id 必须能在 race 索引里找到主人。主键是 rally 原生
    # race-id（uuid），段级 SEG| 键是导出期中间量，合并后不得外流 ——
    # 外流出去的 SEG| race_id 在 race 索引查无此人，自检会假阴性。
    run_ids = {r.get("race_id") for r in runs}
    _orphans, _badfmt = {}, {}
    for m in metrics:
        rid = m.get("race_id")
        if rid is None:
            continue
        if rid not in run_ids:
            _orphans[rid] = _orphans.get(rid, 0) + 1
        if rid.startswith("SEG|"):
            _badfmt[rid] = _badfmt.get(rid, 0) + 1
    for rid, n in sorted(_orphans.items()):
        problems.append("metric 行的 race_id 在 race 索引不存在（被剔场次的行漏闸）: %s × %d 行" % (rid, n))
    for rid, n in sorted(_badfmt.items()):
        problems.append("race_id 是段级键（SEG| 外流）: %s × %d 行" % (rid, n))
    for r in runs:
        if (r.get("race_id") or "").startswith("SEG|"):
            problems.append("race 行主键是段级键: %s" % r.get("race_id"))
    # 覆盖率（非致命）
    n = len(runs) or 1
    def cov(fn):
        return round(100.0 * sum(1 for r in runs if fn(r)) / n, 1)
    cover = {
        "env_id": cov(lambda r: bool(r.get("env_id"))),
        "dims": cov(lambda r: r.get("dims") is not None),
        "codec": cov(lambda r: bool(r.get("codec"))),
        "source_reuse": cov(lambda r: r.get("source_reuse") is not None),
        # 配置列覆盖率（实际生效值）
        "heap_mb": cov(lambda r: r.get("heap_mb") is not None),
        "cores": cov(lambda r: r.get("cores") is not None),
        "trusted_true": sum(1 for r in runs if r.get("trusted") is True),
        "trusted_false": sum(1 for r in runs if r.get("trusted") is False),
        "multi_segment": sum(1 for r in runs if isinstance(r.get("challenge"), list) and len(r["challenge"]) > 1),
    }
    for k, v in cover.items():
        if k != "degraded_races" and v < 50.0:
            warnings.append("字段覆盖率偏低: %s = %.1f%%（过滤条件用该字段会漏掉大半场次）" % (k, v))
    return problems, warnings, {"fields": cover, "stat_histogram": stat_count,
                                "unit_histogram": unit_count, "stat_aliases": aliases}


# ---------------------------------------------------------------------------
# 场次身份：真 fid 与重试号的恢复
# ---------------------------------------------------------------------------
# race.json 里**没有 fid**（导出器用 user-tags 拼的会丢掉重试号 -tryN），
# 真 fid 只存在于两个地方：
#   ① campaign 的 results/<fid>[-tryN]-<phase>-race.json 的**文件名**
#   ② sidecar 的 run.fid
# ① 覆盖更广：文件名里有 race-id，能与 rally store 里的 uuid 目录对上；
# ② 只有一部分场次有 sidecar。恢复只认这两个来源，不靠猜。
_FID_FILE_RE = re.compile(r"^(.*)-(w|q|m|wq)-race\.json$")
_TRY_RE = re.compile(r"-try(\d+)$")


def load_race_id_index(campaigns_dir):
    """race-id → (真 fid, phase)：从 campaign 的 results/ 文件名取。

    文件名是「真 fid」的唯一载体：race.json 内部没有 fid，sidecar 只覆盖 40% 的场次。
    """
    idx = {}
    for p in glob.glob(os.path.join(campaigns_dir, "*", "results", "*-race.json")):
        m = _FID_FILE_RE.match(os.path.basename(p))
        if not m:
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        rid = d.get("race-id")
        if rid:
            idx[rid] = (m.group(1), m.group(2))
    return idx


def track_from_fid(fid):
    """真 fid（{车}-{赛道}-r{轮}[-tryN]）→ 赛道名。

    fid 的赛道段是**唯一可靠**的来源：user_tags.track 在早期批次里被解析器写成了
    键名（字面量 "track"），而 fid 是编排时定的。
    赛道名本身含 '-'（ds-g1920 / hvtp200k-3s），所以用贪婪匹配。
    """
    if not fid:
        return None
    base = _TRY_RE.sub("", fid)
    m = re.match(r"^[^\-]+-(.+)-r\w+$", base)
    return m.group(1) if m else None


# 臂头行里的「臂 → (版本, build)」：PROGRESS.txt 的
# "=== 臂 ez250 (2.5.0-3008) 开始 ===" 与 SUMMARY.md 的 "## ez250 (2.5.0-3008)"。
# 臂名就是车代号 —— 这是 sidecar 之外的第二条 build 证据链。
_ARM_BUILD_RE = re.compile(r"===\s+臂\s+(\S+)\s*\((\d[\d.]*)-(\d+)\)")
_SUMMARY_ARM_RE = re.compile(r"^##\s+(\S+)\s*\((\d[\d.]*)-(\d+)\)", re.M)


def load_arm_builds(camp_dir):
    """批次级的「臂 → build」表。

    没有 sidecar 的批次靠臂头行拿 build（否则只能写 "—"，所有未知 build 的车挤进
    同一条线）。臂头行是**批次级**证据：同一批次里这个臂用的就是这个 build。
    """
    out = {}
    for name, rx in (("PROGRESS.txt", _ARM_BUILD_RE), ("SUMMARY.md", _SUMMARY_ARM_RE)):
        p = os.path.join(camp_dir, name)
        if not os.path.exists(p):
            continue
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in rx.finditer(txt):
            out.setdefault(m.group(1), m.group(3))
    return out


def arm_build_of(arms, car):
    """按车代号取臂级 build；车代号可能带后缀（jdk25-3009），臂名是它的前缀。"""
    if not arms or not car:
        return None
    if car in arms:
        return arms[car]
    cands = [a for a in arms if car.startswith(a + "-")]
    if len(cands) == 1:
        return arms[cands[0]]
    return None


def variant_from_fid(fid):
    """真 fid（{车}-{变体}-r{轮}[-tryN]）→ 变体段（不做归一）。

    变体 id 不入库（track_variant 已退役，覆盖判定由 codec/source_reuse 列派生，
    见 bench-history.variant_id_of）；本函数供溯源/调试读取 fid 中段。
    """
    base = _TRY_RE.sub("", fid or "")
    m = re.match(r"^[^\-]+-(.+)-r\w+$", base)
    return m.group(1) if m else None
    return None


# 赛道名对齐官方 rally-tracks：geonames 就叫 geonames。
# 只作用于**输出层**（canon_track / 入库值）；fid 拼接与 RACES.md 回查仍用历史名
# （TRACK_NAME 不动），否则 jdk25-geo-r1 拼成 jdk25-geonames-r1 查不到记账。
_TRACK_RENAME = {"geo": "geonames"}


def canon_track(raw):
    """原始赛道名 → 规范赛道名：历史短名（gdef/gzstd/gbc）→ 规范变体名
    （geo-codec-*）→ 基础赛道名 → 官方名（geo → geonames）。
    新编排直接用官方名（geonames），原样通过。"""
    if not raw:
        return None
    base = H.base_of(H.canonical_id(str(raw)))
    return _TRACK_RENAME.get(base, base)


def attempt_of(fid):
    """重试号：fid 尾部的 -tryN → N；没有后缀 → 1。"""
    m = _TRY_RE.search(fid or "")
    return int(m.group(1)) if m else 1


def heap_mb_of(tag):
    """编排标签的堆（"16g"）→ MB 整数；不是这个形态就返回 None。"""
    if not tag:
        return None
    m = re.match(r"^(\d+)([gGmM])$", str(tag).strip())
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2).lower() == "g" else n


def codec_norm(c):
    """压缩方式归一：大小写不统一（实测混着 ZSTD 与 default）→ 词表封闭的小写值。"""
    if not c:
        return None
    c = str(c).strip().lower()
    return {"zstd": "zstd", "best_compression": "best_compression",
            "best compression": "best_compression", "default": "default"}.get(c, c)


def sr_bool(v):
    """source_reuse → 裸布尔（契约要求布尔，不是字符串 "true"）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--races", default="/root/.rally/benchmarks/races")
    ap.add_argument("--campaigns", default="campaigns")
    ap.add_argument("--allow-fail", action="store_true",
                    help="口径契约断言失败也照样写出载荷（仅用于排查，默认失败即非零退出）")
    ap.add_argument("--require-campaign", action="store_true",
                    help="只导有 campaign 元数据（RACES.md 状态 + sidecar）的场次。"
                         "rally race store 里混着大量早期 one-off 场次（无 campaign 目录、"
                         "track 名未归一、多数都叫 track），把它们灌进可视层会造出"
                         "一个上千场次的'杂赛道'看板，且这些行 valid 为空 —— "
                         "默认看板闸门会滤掉，但索引里全是噪音。默认不过滤。")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    metas = {}
    for c in sorted(glob.glob(os.path.join(args.campaigns, "*"))):
        if os.path.isdir(c):
            metas[os.path.basename(c)] = load_campaign_meta(c)

    # 召回率探针结果（results/recall-*.json）—— 与 race.json 无关，按 (campaign, fid) 索引
    recall_idx = load_recall_index(args.campaigns)
    _recall_fids = sorted({f for (_c, f) in recall_idx})

    envs, runs, metrics = {}, [], []
    # segments：一次测量的**每一段**（老协议一次测量跑写赛+查赛两段）。
    # 段级先攒着，循环结束后按 (批次, 真fid, 重试号) 合并成一行 race。
    segments = []
    # dropped：被写入侧闸门剔掉的场次清单（manifest 可审计）
    dropped = []
    # race-id → (真 fid, phase)：真 fid 只存在于 campaign 的 results/ 文件名里
    rid2fid = load_race_id_index(args.campaigns)
    counters = {}          # 导出期统计（口径来源分布等），进 manifest
    # (campaign, fid) → 场次回填材料（track/时间/valid/quality/series_key），
    # monitors 补的 system 行从这里取自己批次的值。
    # 键必须带 campaign：fid（{car}-{track}-r{round}）**只在一个 campaign 内唯一**，
    # 跨 campaign 反复出现 —— 用跨批次全局字典 + setdefault（先到先得）会把 system 行
    # 挂到别的批次的场次上（批次跨度虚报）。
    camp_fid = {}
    seen = set()
    unmatched = 0
    skipped_no_campaign = 0
    # ---- 批次锚点预扫描：campaign → 该批次最早一场 race 的时刻 ----
    # 一次发压批次可能跨 UTC 午夜，按 @timestamp 分桶会把**一次实验切成两段**，
    # 写入阶段与查询阶段落到不同日桶，日均值因此塌陷。
    # run_start 给每行一个批次锚点：同一批次的所有行共享同一个值，按它分桶恒为一个桶。
    # 不落 run_day（本地日）：批次归属日可由 run_start + 时区折算，落一个派生列只会多一处
    # 口径分叉；run_start 是绝对时刻，唯一真相。
    store = list(load_store(args.races))
    campaign_run_start = {}
    for _d in store:
        _camp = ((_d.get("user-tags") or {}).get("campaign")) or "unknown"
        _ts = _d.get("race-timestamp")
        if not _ts:
            continue
        _iso = datetime.datetime.strptime(_ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
        if _camp not in campaign_run_start or _iso < campaign_run_start[_camp]:
            campaign_run_start[_camp] = _iso
    for d in store:
        ut = d.get("user-tags") or {}
        camp = ut.get("campaign") or "unknown"
        car, tr, _ = fid_of(d)
        phase = PHASE.get(d.get("challenge"), "wq")
        rnd = ut.get("round")
        fid = "%s-%s-r%s" % (car, tr, rnd)
        meta = metas.get(camp, {})
        race_id = d.get("race-id") or os.path.basename(os.path.dirname(d["_path"]))
        if race_id in seen:
            continue
        if args.require_campaign and camp not in metas:
            skipped_no_campaign += 1
            continue
        seen.add(race_id)
        ts = d.get("race-timestamp")
        ts = (datetime.datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
              if ts else None)
        side = meta.get("sidecar_by_race", {}).get(race_id) or meta.get("sidecar", {}).get(fid + "-" + phase, {})
        # ---- 真 fid 与重试号的恢复（见 load_race_id_index 的说明）----
        # 真 fid 只在 campaign 的 results/ 文件名与 sidecar.run.fid 里，race.json 自己没有。
        # 优先用文件名（覆盖全量、含 -tryN），sidecar 作为第二路。
        _rid_fid = rid2fid.get(race_id)
        real_fid = (_rid_fid[0] if _rid_fid else None) \
            or ((side.get("run") or {}).get("fid") if side else None)
        attempt = attempt_of(real_fid or fid)
        # status 按真 fid 回查 RACES.md：重试场的 fid 带 -tryN，
        # 否则 10-18 的 es819-gbc-r1-try3（VALID）会被套上 r1 首 attempt 的 QUERY-FAIL
        status = (meta.get("status", {}).get(real_fid) if real_fid else None) \
            or meta.get("status", {}).get(fid) or meta.get("status", {}).get(fid + "-try2") \
            or meta.get("status", {}).get(fid + "-try3")
        if real_fid:
            fid = real_fid   # 真 fid 才是本场唯一身份（含变体名与 -tryN），比 car-track-rN 拼出来的准
        # 赛道名有两条来源，**谁命中规范名登记表就用谁**：
        #   ① 真 fid 的赛道段 —— user_tags.track 在早期批次里是解析器写坏的字面量
        #      （"track"），而 fid 是编排时定的，可靠；
        #   ② rally 记的 track 字段 —— 有几场它的 fid 形态本身是脏的
        #      （车代号段多带了 build 号），但 track 字段是规范名。
        # 两条都不命中 → 归一不出来，这场不写库（见写入侧闸门）。
        _tr_fid = canon_track(track_from_fid(fid))
        _tr_tag = canon_track(d.get("track"))
        tr = (_tr_fid if _tr_fid in CANON_TRACK
              else (_tr_tag if _tr_tag in CANON_TRACK else (_tr_fid or _tr_tag)))
        dur = None
        for om in ((d.get("results") or {}).get("op_metrics") or []):
            if om.get("task") == "index-append" and om.get("duration"):
                dur = om["duration"] / 1000.0
        rate_b = (DOCS.get(d.get("track"), 0) / dur) if dur and DOCS.get(d.get("track")) else None
        warn = []
        eng_tag = ut.get("engine") or ""
        for _cf in (meta.get("contaminated") or []):
            if _cf == fid:
                warn.append("受宿主旁路负载污染：该场写赛窗口内落过宿主侧全量导出，吞吐可能偏低，结论以重跑为准")
                break
        if camp.startswith("ds3-") and eng_tag != "es819":
            warn.append("heap 声明不实：user_tags heap:16g 但 ds3 campaign 的 easysearch 车实测 -Xmx1g，绝对值不可与他批比较")
        if not side:
            warn.append("no-sidecar：JDK/heap/clients/bulk 无实测值，仅有 user_tags 声明")
        if status is None:
            warn.append("no-RACES.md：valid 未知（campaign 目录不在或该场未记账）")

        # ---- 写入侧闸门：不该入库的场次根本不写库（契约 §2.0）----
        # 库里没有 valid / status，因为能查到的都是过了筛的：
        #   ① 非 VALID（RACES.md 记 SKIP / QUERY-FAIL，或根本没记账）→ 整场不写
        #   ② host-noise（CONTAMINATED.txt 人工登记：宿主旁路负载污染）→ 整场不写
        # 场次级过滤放在写入侧，而不是"入库后再让每个面板记得加过滤"——
        # 后者漏一个面板就混进脏数据。
        # 被剔的场次在 manifest 的 dropped 段留清单，可审计。
        _drop_reason = None
        if status != "VALID":
            _drop_reason = "not_valid"
        elif fid in (meta.get("contaminated") or []):
            _drop_reason = "host_noise"
        elif tr not in CANON_TRACK:
            _drop_reason = "track_unknown"

        # ---- 实测身份（方案 §3.2「引擎(实测)」+ §3.2「实验变量 typed promote」）----
        # 身份只认**本场/本车/本race 的源头证据**：sidecar 实测 > 部署日志 >
        # revision 指纹 > distribution-flavor/版本 > 车名兜底。
        # 不设跨场次回填池（campaign 不是证据池）：包指纹无本场实测就是 null，
        # JDK 完整版本走 identity.py 的车级实测登记表（JDK 与车代号绑定、跨部署期不变）。
        se = side.get("engine") or {}
        sj = side.get("jdk") or {}
        srp = side.get("run_params") or {}
        # 身份来源按可信度排队：sidecar 实测 > 部署日志 > revision 指纹 > 批次清单 > 车名兜底。
        # 批次清单（PACKAGES.txt）排最后：它是"这批装了哪些包"，不是"这个车跑的哪个包"。
        deploys = meta.get("deploys") or {}
        deploy = deploys.get(car)                       # 车级部署记录
        rev = (d.get("cluster") or {}).get("revision")   # 引擎源码指纹
        rev_id = REVISION_IDENTITY.get(rev) if rev else None
        pkg = se.get("pkg")                             # 车实际用的包（sidecar 实测），没有就是没有
        base_pkg = os.path.basename(pkg or "")
        engine = ("elasticsearch" if base_pkg.startswith("elasticsearch")
                  else "easysearch" if base_pkg.startswith("easysearch") else None)
        if not engine and rev_id:
            engine = rev_id[0]
        if not engine:
            # 没有 sidecar 的 campaign：rally 记的 distribution-flavor
            # 足以区分产品线 —— easysearch=oss，elasticsearch=default
            flavor = (d.get("cluster") or {}).get("distribution-flavor")
            engine = {"oss": "easysearch", "default": "elasticsearch"}.get(flavor)
        if not engine and deploy:
            engine = deploy[0]
        if not engine:
            engine = ("easysearch" if (car or "").startswith("ez")
                      else "elasticsearch" if (car or "").startswith("es") else None)
        # 引擎级部署记录（ibs-deploy-easysearch.log / deploy-elasticsearch.log）：
        # 该批所有同引擎的车共用一份部署
        deploy_eng = deploys.get(engine) if engine else None
        vm = se.get("version_measured_nodes") or []
        version = ((vm[0] if vm else None) or se.get("version_from_pkg")
                   or (deploy[1] if deploy else None)
                   or (deploy_eng[1] if deploy_eng else None)
                   or (rev_id[1] if rev_id else None)
                   or (d.get("cluster") or {}).get("distribution-version"))
        # build（包名主干）与 version_from_pkg 不再产出：与 pkg / version /
        # engine_build 完全冗余且全仓零消费方。
        # 构建号（3015 / 3009 …）：version 字段只有主次版本（如 2.5.0），无法区分
        # 跨 build 的两批数据。ds3 首轮是 2.5.0-3009、ds2 是 2.5.0-3015，
        # 车标签相同（jdk25/jdk26/jdk27）时看板上会混成一条线，必须能按 build 过滤。
        vfp = se.get("version_from_pkg") or ""
        _mb = re.search(r"-(\d+)-linux", pkg or "")
        build_no = _mb.group(1) if _mb else None
        if not build_no and vfp:
            build_no = vfp.rsplit("-", 1)[-1] if "-" in vfp else None
        if not build_no and deploy:
            build_no = deploy[2]
        if not build_no and rev_id:
            build_no = rev_id[2]
        if not build_no and deploy_eng:
            build_no = deploy_eng[2]
        if not build_no:
            # 臂头行（PROGRESS.txt / SUMMARY.md）：批次级的「这个臂用的哪个 build」。
            # 优先级在 user_tags.engine_ver 之前 —— 车标签是编排时贴的，
            # 臂头行是那一批实际装的东西。
            build_no = arm_build_of(meta.get("arm_builds"), car)
        if not build_no:
            _uv = ut.get("engine_ver") or ""
            build_no = (_uv.rsplit("-", 1)[-1] if "-" in _uv else None) or None
        # ---- JDK 身份：实测优先，逐级降级（identity.py 口径）----
        # ① sidecar 本场实测（vendor_detected + measured_nodes）
        # ② campaign/logs/jdk-<车>.txt 的 java -version 原文
        # ③ 车代号兜底表（CAR_FALLBACK：厂商/主版本）+ JDK 版本登记表（完整版本号）
        # jdk_vendor 与 jdk_version 分开存：GraalVM 25 与 Kona 25 的完整版本号
        # 都是 25.0.4，只有厂商能分 —— 而 JDK 厂商对比正是本项目要回答的问题。
        jdk_file = (meta.get("jdks") or {}).get(car)
        jdk_vm = jdk_vm_of(sj, jdk_file)
        jdk_vendor = jdk_vendor_of(side, car, jdk_vm=jdk_vm, jdk_file=jdk_file)
        jdk_major = jdk_major_of(side, car, jdk_vm=jdk_vm, jdk_file=jdk_file)
        jdk_version = jdk_version_of(side, jdk_file=jdk_file, car=car)
        if not jdk_major:
            jdk_major = (str(jdk_version).split(".")[0] if jdk_version else None)
        # 系列键（看板分线键 + 图例文案）：引擎@版本@build@JDK
        # JDK 段查不到时按车代号写 unk-<车>，避免未实测的车塌成同一条 "?"
        series_key = series_key_of(engine, version, build_no, jdk_vendor, jdk_major, car=car)
        heap_max = srp.get("heap_max_measured_mb") or []
        heap_gb = round(max(heap_max) / 1024, 2) if heap_max else None
        tp = d.get("track-params") or {}
        # 实验变量：维度/段形态 —— 从 user-tags 与 track params 提升为 typed 列（方案 §3.2）
        # dims 的来源顺序（§五·17 定案）：编排 user-tags → track params → **侧车实测**
        # knn_params.dims → 赛道登记表兜底。侧车实测排在登记表前面：它是本场实际建索引
        # 用的维度，登记表只是赛道默认值。
        _knn_dims = (knn_params_of(side) or {}).get("dims")
        dims = ut.get("dim") or tp.get("dim") or _knn_dims
        segment_form = ut.get("segment_form") or tp.get("segment_form")

        # ---- 场次可信度 ----
        # RACES.md 的 status 只反映"这场跑完了没有"，不反映"结果可不可用"。
        # 某个 operation error_rate=1.0（该 task 全部请求失败）时，status 仍是 VALID
        # → valid:true → 这些失败 task 的"漂亮延迟"（失败响应很快）被混进查询延迟曲线，
        # 把引擎显得比实际更快。
        # 这里按 task 逐个判 error_rate，场次级取最差；行级 quality 由 emit_metrics 落。
        op_metrics = (d.get("results") or {}).get("op_metrics") or []
        task_quality_map = {}
        for om in op_metrics:
            t = om.get("task") or om.get("operation") or "?"
            task_quality_map[t] = task_quality(om, t)
        degraded_tasks = sorted(t for t, q in task_quality_map.items() if q == "degraded")
        if degraded_tasks:
            race_quality = "degraded"
        elif status is None:
            race_quality = "unknown"
        else:
            race_quality = "ok"
        # 赛级指标（store_size / segment_count / GC / merge…）量的是**写阶段**的状态，
        # 一个查询 task 失败不该把它们一起降级 —— 否则磁盘与 GC 数据会因为一个查询
        # task 挂了而从看板上消失。只有写阶段自己失败才降级。
        write_degraded = any(is_write_task(t) for t in degraded_tasks)
        race_row_quality = "degraded" if write_degraded else "ok"

        # ---- common = 场次级 + 指标行共用的维度 ----
        # 身份维度只在 benchmark-result-race 存全套（engine/version/engine_build/jdk_vendor/
        # jdk_version/series_key）；benchmark-result-metric 只带 series_key（看板分线的唯一依赖）。
        # 车代号（car/arm/car_build/car_label）不入库 —— 那是「车代号」的重复编码，
        # 车代号只留在 user_tags.car，身份靠上面六个正交字段表达。
        # ---- 段级标识：一次测量 = (批次 × 真fid × 重试号) ----
        # seg_key 是导出期中间量；合并后映射到最终 race_id。
        seg_key = "SEG|%s|%s|a%d" % (camp, fid, attempt)
        # ---- common = 场次级 + 指标行共用的维度 ----
        common = {"race_id": seg_key, "campaign": camp,
                  # run_start：批次锚点（该批次最早一场 race 的时刻）——
                  # 同一批次的所有行共享同一个值，按它分桶恒为一个桶，跨午夜批次不被切开。
                  "run_start": campaign_run_start.get(camp),
                  # series_key：看板分线键 + 图例文案（引擎@版本@build@JDK）
                  "series_key": series_key,
                  # 实验变量：这一场测的是什么（向量维度）
                  "dims": dims,
                  # 处理变量：归一后的压缩方式 + 裸布尔 source_reuse。
                  # 两者都进 metric —— 面板按 series_key 分线，ES 不能 join，
                  # 指标行不带它们就没法排除存储变体（DESIGN §2.6）。
                  "codec": codec_norm(codec_of(d, side)),
                  "source_reuse": sr_bool(source_reuse_of(d, side)),
                  "trusted": not degraded_tasks,
                  # track 必须进 common：benchmark-result-metric 的指标行没有它就没法按赛道过滤，
                  # 而按赛道拆看板要求每个指标行都带归一化后的赛道名
                  "track": tr, "phase": phase,
                  "env_id": (side.get("env") or {}).get("env_id")}
        # ---- 场次行（段级；同一测量的多段在循环后合并成一行）----
        _knn = knn_params_of(side) or {}
        _mis = measured_index_settings(side)
        # 语料兜底：sidecar 缺席时按 track 注册表补 name/docs（与 tracks/ 定版对齐；
        # hvtp200k 一次性批次不登记，保持 null 不臆造）
        _corpus = side.get("corpus")
        if _corpus is None:
            _cn, _cd = CORPUS_NAME.get(d.get("track")), DOCS.get(d.get("track"))
            _corpus = {"name": _cn, "docs": _cd} if (_cn or _cd) else {}
        _cluster = d.get("cluster") or {}
        # 配置列（实际生效值）：编排标签优先，sidecar 声明兜底 ——
        # 同一事实的两条来源路径，不是两列（"一个事实只占一列"）。
        # 只记一层的前提：编排侧 heap_assert 起跑前断言实际生效堆=标签（fail-fast）。
        _cfg_heap = heap_mb_of(ut.get("heap")) or srp.get("heap_declared_mb")
        _cfg_cores = ut.get("cores") or srp.get("cores_declared")
        # 赛末体积：探针落的是 GiB，契约统一用 GB（×1.073741824）。
        _pm = (meta.get("post_merge", {}).get(real_fid or fid) or {})
        _pm_store_gb = (round(_pm["store_gb"] * 1.073741824, 4)
                        if _pm.get("store_gb") is not None else None)
        seg_row = {"_index": "benchmark-result-race", "_id": seg_key, "race_id": seg_key,
                   "@timestamp": ts, "campaign": camp,
                   # run_start：批次锚点 —— 跨 UTC 午夜的批次按它归到一个桶
                   "run_start": campaign_run_start.get(camp),
                   # ---- 身份维度（正交，唯一事实源在本索引）----
                   "engine": engine,
                   "version": str(version) if version is not None else None,
                   "engine_build": build_no,
                   "jdk_vendor": jdk_vendor, "jdk_version": jdk_version,
                   "series_key": series_key, "car_code": car,
                   # ---- 配置（实际生效值，单层）----
                   "heap_mb": int(_cfg_heap) if _cfg_heap is not None else None,
                   "cores": int(_cfg_cores) if _cfg_cores is not None else None,
                   # ---- 包指纹（sha256 只认本场 sidecar 实测，不跨批次借）----
                   "pkg": pkg, "pkg_sha256": (side.get("engine") or {}).get("pkg_sha256"),
                   # ---- run 维度 ----
                   "track": tr,
                   "challenge": d.get("challenge"), "round": rnd,
                   "dims": dims,
                   "codec": common["codec"], "source_reuse": common["source_reuse"],
                   "env_id": (side.get("env") or {}).get("env_id"),
                   "nnodes": ut.get("nnodes"),
                   "clients": srp.get("clients"), "bulk_size": srp.get("bulk_size"),
                   "shards": (srp.get("shards") if srp.get("shards") is not None
                              else _mis.get("shards")),
                   "replicas": (srp.get("replicas") if srp.get("replicas") is not None
                                else _mis.get("replicas")),
                   # ---- 向量参数（flattened 拆出的标量）----
                   "knn_index_type": _knn.get("index_type"),
                   "knn_element_type": _knn.get("element_type"),
                   "knn_m": _knn.get("m"), "knn_ef_construction": _knn.get("ef_construction"),
                   "knn_num_candidates": _knn.get("num_candidates"),
                   # ---- 语料与环境（flattened 拆出的标量）----
                   "corpus_docs": _corpus.get("docs"), "corpus_name": _corpus.get("name"),
                   "cluster_revision": _cluster.get("revision"),
                   "cluster_flavor": _cluster.get("distribution-flavor"),
                   "rally_version": d.get("rally-version"),
                   # ---- 赛末快照 + 溯源 + 可信度 ----
                   # 赛末 forcemerge 后的快照：另一个时间点的测量，不是场次值，
                   # 画时间序列会失真 —— 只用于报表读数，不做看板聚合。
                   # 单位换算：探针落的是 GiB，契约统一用 GB（×1.073741824）。
                   "store_size_gb_post_merge": _pm_store_gb,
                   "segments_post_merge": (meta.get("post_merge", {}).get(real_fid or fid) or {}).get("segments"),
                   "source": [d["_path"], "campaign:" + camp] + (["sidecar:" + fid + "-" + phase] if side else []),
                   "trusted": (not degraded_tasks)}
        if _drop_reason:
            dropped.append({"reason": _drop_reason, "campaign": camp, "fid": fid,
                            "attempt": attempt, "status": status,
                            "track": tr, "timestamp": ts})
        else:
            segments.append({"key": (camp, fid, attempt), "seg_key": seg_key, "phase": phase,
                             "ts": ts, "row": seg_row, "warn": list(warn),
                             "rally_id": d.get("race-id")})
            emit_metrics(d, seg_key, ts, common, metrics, side=side, counters=counters)
            # ---- 召回率行：挂到该场**查询** race 上 ----
            # 探针在查询跑完后离线执行，结果按 (campaign, fid) 索引；
            # fid 带 -tryN 变体时按前缀兜底（recall 文件用原始 fid 命名）。
            # 必须和 emit_metrics 一样待在闸门的 else 里 —— 放在闸门外，
            # 被剔场次的 recall 行照写不误，SEG| race_id 在 race 索引查无此人。
            # 召回率行：**查询相**的场次才发。
            # phase=="q" 是老协议（读写分跑）；phase=="wq" 是单挑战 `full` 协议
            # （读写同一段跑）—— 查询相就在同一段里。两个都要判，
            # 只判 q 会把 full 协议场次的 recall 行全漏了（"数据在库里但从没被查过"）。
            if phase in ("q", "wq"):
                _rec = recall_idx.get((camp, fid))
                if not _rec:
                    for (_c2, f2), r2 in recall_idx.items():
                        if _c2 == camp and fid.startswith(f2 + "-"):
                            _rec = r2
                            break
                for _task, _stats in (_rec or {}).items():
                    # recall 行的 trusted 取**它自己那个 task** 的判定，不是整场的判定。
                    # 场级 trusted 是"任一 task 坏了就整场不可信"，而 recall 只回答
                    # 一个 task 的召回率。两者混用会让同一 (场次, task) 出现 trusted
                    # 不一致的行 —— 与同 task 的时延行打架，导出期断言直接拒收整批。
                    # 探针的 task 名与 op_metrics 的 task 名同源（RECALL_TASK_BY_K），
                    # 对不上就回落场级判定。
                    _q = task_quality_map.get(_task)
                    _trusted = (common.get("trusted") if _q is None
                                else (_q != "degraded"))
                    for _stat, _v in _stats.items():
                        metrics.append({"_index": "benchmark-result-metric",
                                        "_id": "%s|%s|%s|recall|%s" % (seg_key, phase, _task, _stat),
                                        "@timestamp": ts, "task": _task, "operation": None,
                                        "family": "recall", "metric": "recall", "stat": _stat,
                                        "value": _v, "unit": "ratio",
                                        "value_original": _v, "unit_original": "ratio",
                                        "direction": "higher-better",
                                        **{k: v for k, v in common.items() if k in METRIC_FIELDS},
                                        "trusted": _trusted})
        if side:
            e = side.get("env") or {}
            eid = e.get("env_id")
            if eid and eid not in envs:
                envs[eid] = {"_index": "benchmark-result-env", "_id": eid, "env_id": eid,
                             "host": e.get("host"), "vm_nodes": e.get("vm_nodes"),
                             "topology": {"node_count": len(e.get("vm_nodes") or []),
                                          "heap_gb": heap_gb, "gc": "G1",
                                          "cpu_pinning": "none" if all(
                                              str(n.get("cpu_pinning_hypervisor")) in ("0", "None", "")
                                              for n in (e.get("vm_nodes") or [{}])) else "pinning"},
                             "host_type": env_host_type(eid, e.get("vm_nodes")),
                             "vcpu": env_vcpu(e.get("vm_nodes")),
                             "mem_gb": env_mem_gb(e.get("vm_nodes")),
                             "cpu_model": (e.get("host") or {}).get("cpu_model"),
                             "disk_type": None, "disk_path": None,
                             "os": env_os(e.get("vm_nodes")),
                             "kernel": (e.get("host") or {}).get("kernel"),
                             "notes": env_notes(e, env_host_type(eid, e.get("vm_nodes"))),
                             "created_at": ts, "active": True,
                             "source": side.get("source")}
            elif eid:
                # 同一环境跨 campaign 复用：堆取各场实测最大值，拓扑以信息更全的一侧为准
                cur = envs[eid]
                t = cur.setdefault("topology", {})
                if heap_gb and (t.get("heap_gb") is None or heap_gb > t["heap_gb"]):
                    t["heap_gb"] = heap_gb
                if not cur.get("kernel"):
                    cur["kernel"] = (e.get("host") or {}).get("kernel")
        elif camp not in metas:
            unmatched += 1

    # ---- 合并：同一次测量的多个段合成一行 race ----
    # 拆分协议一次测量跑两段（写赛 + 查赛），存成两场；单挑战协议一次跑完只有一段。
    # 合并后「一次比赛 = 一行」。赛级指标（results 块：disk/gc/merge/throttle）也只剩一份 ——
    # 两段实测同值，留两份就是重复计数。
    seg_groups = {}
    for seg in segments:
        seg_groups.setdefault(seg["key"], []).append(seg)
    # 段级警告汇总（caliber_warnings 字段已退役，警告改走 manifest）
    _warn_count = collections.Counter()
    for seg in segments:
        for w in (seg.get("warn") or []):
            _warn_count[w.split("：")[0]] += 1
    seg_warnings = ["场次警告 %s：%d 场" % (_k, _v) for _k, _v in sorted(_warn_count.items())]
    seg2race = {}          # 段级 key → 合并后的 race_id
    seg2primary = {}       # 合并后的 race_id → 主段的 phase（段级 key 不含 phase，用行内 phase 区分来源段）
    race2ts = {}           # 合并后的 race_id → 测量起点（最早段的时刻）
    dropped_race_level = 0
    for key, segs in seg_groups.items():
        # 主段：写段优先（赛级指标量的是写阶段结束时的状态），否则最早的一段
        segs_sorted = sorted(segs, key=lambda x: (x["phase"] != "w", x["ts"] or ""))
        primary = segs_sorted[0]
        # 主键照搬 rally 原生 race-id：uuid 存在 race.json 文件里，重导出不变 →
        # upsert 幂等成立。
        # 两段测量主键取主段（写段优先）的，另一段的 uuid 在 source 溯源列里；
        # 单挑战 full 一场一段，主键与 rally id 一一对应。
        # 兜底：极老 race.json 缺 race-id 时退回业务键（重导出核对覆盖率）。
        race_id = primary.get("rally_id") or "%s|%s|a%d" % (key[0], key[1], key[2])
        row = dict(primary["row"])
        row["_id"] = race_id
        row["race_id"] = race_id
        # challenge 是多值：这次测量由哪些 challenge 段组成（值全是源值，不新增词表）
        row["challenge"] = sorted({x["row"]["challenge"] for x in segs if x["row"].get("challenge")})
        # 场次时间 = 最早一段的时刻（一次测量的起点）
        _tss = [x["ts"] for x in segs if x["ts"]]
        row["@timestamp"] = min(_tss) if _tss else None
        race2ts[race_id] = row["@timestamp"]
        # 可信度：任一段不可信 → 整场不可信
        row["trusted"] = all(x["row"].get("trusted") for x in segs)
        # 溯源：各段的路径合并
        _src = []
        for x in segs:
            for p in (x["row"].get("source") or []):
                if p not in _src:
                    _src.append(p)
        row["source"] = _src
        runs.append(row)
        seg2primary[race_id] = primary["phase"]
        for x in segs:
            seg2race[x["seg_key"]] = race_id
        # system 行的回填材料：按 (campaign, 真fid) 归档 —— 同批次内真fid ↔ 一次测量一一对应
        # （重试场的真fid 带 -tryN，仍是唯一身份）。
        camp_fid[(key[0], key[1])] = {"track": row["track"], "ts": row["@timestamp"],
                                     "race_id": race_id, "trusted": row["trusted"],
                                     "series_key": row["series_key"]}

    # ---- 指标行：段级 key → 合并后的 race_id；赛级指标只留主段的那一份 ----
    _kept = []
    for m in metrics:
        sid = m.get("race_id")
        rid = seg2race.get(sid)
        if rid is None:
            _kept.append(m)
            continue
        # 赛级指标（无 task）在写段与查段各产出一份且数值完全相同 → 只留主段那份，
        # 否则按 race_id 聚合会重复计数（DESIGN §五·11 的根因）。
        # 注意段级 seg_key 不含 phase（同一测量的两段共用），来源段要看行内的 phase。
        if m.get("task") is None and m.get("phase") != seg2primary.get(rid):
            dropped_race_level += 1
            continue
        if m.get("_id", "").startswith(sid + "|"):
            m["_id"] = rid + m["_id"][len(sid):]
        m["race_id"] = rid
        # 时间统一到测量起点（与 race 行同款）：之前各行带的是来源段的 rally 时刻，
        # 同一测量的写/查行带两个时间戳（实测 188/197 个测量），按 @timestamp
        # 分桶会把一场比赛切进两个桶，与 race 行对不上。
        _mts = race2ts.get(rid)
        if _mts is not None:
            m["@timestamp"] = _mts
        _kept.append(m)
    metrics = _kept

    # monitors → system 指标（benchmark-result-metric 补一行/场/节点/相位/项）
    sysrows = 0
    orphan_fids = {}       # campaign → 该批次 monitors 里对不上本批次任何场次的 fid
    orphan_rows = 0        # 因孤儿 fid 被丢弃的 system 行数
    for camp, meta in metas.items():
        for key, vals in (meta.get("mon") or {}).items():
            # 摘要文件名是 <fid>-<虚机名>-summary.txt，而 fid 自己就带 "-"（es819-gbc-r1-try3），
            # 必须按尾部的 -test-worker-N 切，否则 fid 被截成 "es819"，环境行与场次对不上
            m = re.match(r"^(.*)-(test-worker-\d+)$", key)
            fid, node = (m.group(1), m.group(2)) if m else (key.partition("-")[0], key.partition("-")[2])
            # 相位后缀 -w/-m/-q/-wq（分相位或单挑战全程采集，与 race 行 phase 同词汇）；
            # 剥掉后缀让环境行与 race 行同 fid 对上，相位记进 phase 字段。
            # 报告侧按 fid 聚合取最坏即自动跨相位；无后缀的 campaign → phase=""
            ph = ""
            if fid.endswith("-wq"):
                ph, fid = "wq", fid[:-3]
            elif fid.endswith(("-w", "-m", "-q")):
                ph, fid = fid[-1], fid[:-2]
            # 该 campaign 里找不到这场 race → monitors/ 目录被别的批次的产物污染
            # （服务器侧目录混放）。这种行的时间戳/身份只能取自别的批次（串味），
            # 宁缺毋滥：直接丢弃，manifest 记数。治本 = 清理源层 monitors/。
            src = camp_fid.get((camp, fid))
            if src is None:
                orphan_fids.setdefault(camp, set()).add(fid)
                orphan_rows += sum(1 for mv in (vals or {}).values() if mv is not None)
                continue
            for mk, mv in (vals or {}).items():
                if mv is None:
                    continue
                sysrows += 1
                # env_verdict 是 OK/FAIL 这类**文字**判定，不是数值 —— 塞进 double 的 value 列
                # 会被 ES 拒收（mapper_parsing_exception），整行静默丢失。改走 value_text 文字列。
                if isinstance(mv, str):
                    metrics.append({"_index": "benchmark-result-metric", "_id": "env|%s|%s|%s|value" % (camp, key, mk),
                                    "@timestamp": src["ts"], "campaign": camp, "node": node,
                                    # race_id 指回这次测量（合并后的主键）——
                                    # 无 race_id 的旧 system 行只能靠文件名反查，孤儿行查不到就丢。
                                    "race_id": src["race_id"],
                                    # run_start：system 行也归属该批次 → 同一个锚点，
                                    # 否则同一批次里一半行有锚点一半没有，按它分桶会裂成两个桶。
                                    "run_start": campaign_run_start.get(camp),
                                    "series_key": src["series_key"],
                                    "trusted": src["trusted"],
                                    "phase": ph or None, "track": src["track"],
                                    "family": "system", "metric": mk, "stat": "value",
                                    "value": None, "value_text": mv, "unit": "text",
                                    "value_original": None, "direction": "lower-better"})
                    continue
                metrics.append({"_index": "benchmark-result-metric", "_id": "env|%s|%s|%s" % (camp, key, mk),
                                "@timestamp": src["ts"], "campaign": camp, "node": node,
                                "race_id": src["race_id"],
                                "run_start": campaign_run_start.get(camp),
                                "series_key": src["series_key"],
                                "trusted": src["trusted"],
                                "phase": ph or None, "track": src["track"],
                                "family": "system", "metric": mk, "stat": "value",
                                "value": (mv / 100.0 if unit_of(mk) == "ratio" else mv),
                                "unit": unit_of(mk),
                                "value_original": mv, "direction": direction_of_system(mk)})

    # ---- 口径契约断言：先验后写，脏载荷不进库 ----
    problems, warnings, cover = validate_payload(runs, metrics, envs.values())
    warnings.extend(seg_warnings)
    if orphan_fids:
        warnings.append("monitors 目录里对不上本批次任何场次的 fid（system 行已丢弃，治本=清理源层 monitors/）: %s"
                        % {c: sorted(f) for c, f in sorted(orphan_fids.items())})
    if problems:
        print("!! 口径契约断言失败（%d 项）—— 载荷未写出：" % len(problems))
        for p in problems[:30]:
            print("   - %s" % p)
        if len(problems) > 30:
            print("   ... 其余 %d 项见 manifest" % (len(problems) - 30))
        if not args.allow_fail:
            with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"generated_utc": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "assertions": {"status": "fail", "problems": problems, "warnings": warnings},
                           "coverage": cover}, f, ensure_ascii=False, indent=1)
            return 2
    for w in warnings:
        print("   提示: %s" % w)

    with open(os.path.join(args.out, "benchmark-result-race.jsonl"), "w", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(args.out, "benchmark-result-metric.jsonl"), "w", encoding="utf-8") as f:
        for r in metrics:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(args.out, "benchmark-result-env.jsonl"), "w", encoding="utf-8") as f:
        for r in envs.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _TS = {"type": "date", "format": "strict_date_optional_time||epoch_millis"}

    def _strict(idx, props):
        return {"index_patterns": [idx], "template": {"settings": {"number_of_shards": 1,
                "number_of_replicas": 0}, "mappings": {"dynamic": "strict", "properties": props}}}

    # benchmark-result-race：独立模板 + dynamic strict（D10 定稿：mapping 是设计出来的）。
    # 字段集 = RACE_FIELDS 白名单，逐字段显式声明；标量列给准确类型，
    # 自由形态的对象列（user_tags/cluster/jdk_want…）一律 flattened ——
    # 键随批次演进，声明成 object 会在 strict 下因新键拒收。
    TPL_RACE = _strict("benchmark-result-race", {
        "@timestamp": dict(_TS),
        # run_start：批次锚点（该批次最早一场 race 的时刻）—— date 类型，
        # 能直接当时序图 x 轴（date_histogram 按它分桶，同一批次恒聚一个桶），也能范围过滤。
        "run_start": dict(_TS),
        # ---- 主键与归属 ----
        "race_id": {"type": "keyword"}, "campaign": {"type": "keyword"},
        # ---- 身份正交六列 + 车代号 ----
        # jdk_vendor 必须单列：GraalVM 25 与 Kona 25 的完整版本号都是 25.0.4，只有厂商能分；
        # jdk_version 是实测完整版本号，粒度随 JDK 本身（有更新尾号的 25.0.4，GA 首版就是 27）
        "engine": {"type": "keyword"}, "version": {"type": "keyword"},
        "engine_build": {"type": "keyword"},
        "jdk_vendor": {"type": "keyword"}, "jdk_version": {"type": "keyword"},
        "series_key": {"type": "keyword"}, "car_code": {"type": "keyword"},
        # ---- 配置（实际生效值，单层；漂移检查由编排 heap_assert 起跑前承担）----
        "heap_mb": {"type": "integer"},
        "cores": {"type": "integer"},
        # ---- 包指纹 ----
        "pkg": {"type": "keyword"}, "pkg_sha256": {"type": "keyword"},
        # ---- run 维度 ----
        "track": {"type": "keyword"},
        # challenge 是多值 keyword：一次测量可由多个 challenge 段组成，值全是源值
        "challenge": {"type": "keyword"},
        # typed promote：dims 要能 range/排序，round 要能当整数分组
        "dims": {"type": "integer"}, "round": {"type": "integer"},
        "codec": {"type": "keyword"}, "source_reuse": {"type": "boolean"},
        "env_id": {"type": "keyword"}, "nnodes": {"type": "integer"},
        "clients": {"type": "integer"}, "bulk_size": {"type": "integer"},
        "shards": {"type": "integer"}, "replicas": {"type": "integer"},
        # ---- 向量参数（flattened 拆出的标量）----
        "knn_index_type": {"type": "keyword"}, "knn_element_type": {"type": "keyword"},
        "knn_m": {"type": "integer"}, "knn_ef_construction": {"type": "integer"},
        "knn_num_candidates": {"type": "integer"},
        # ---- 语料与环境（flattened 拆出的标量）----
        "corpus_docs": {"type": "long"}, "corpus_name": {"type": "keyword"},
        "cluster_revision": {"type": "keyword"}, "cluster_flavor": {"type": "keyword"},
        "rally_version": {"type": "keyword"},
        # ---- 赛末快照 + 溯源 + 可信度 ----
        "store_size_gb_post_merge": {"type": "double"},
        "segments_post_merge": {"type": "integer"},
        # source 是字符串数组（溯源路径），不是对象 → keyword（数组按多值索引），
        # 不能用 flattened（flattened 只收对象，标量串会被 mapper_parsing_exception 整批拒收）
        "source": {"type": "keyword"},
        "trusted": {"type": "boolean"},
    })
    # benchmark-result-env：独立模板 + dynamic strict（D10 定稿）
    # disk_type/disk_path 目前恒 null（sidecar 未采集）——列先占住，采集补上后无需改模板
    TPL_ENV = _strict("benchmark-result-env", {
        "env_id": {"type": "keyword"}, "host": {"type": "flattened"},
        "host_type": {"type": "keyword"}, "vcpu": {"type": "integer"},
        "mem_gb": {"type": "double"}, "cpu_model": {"type": "keyword"},
        "disk_type": {"type": "keyword"}, "disk_path": {"type": "keyword"},
        "os": {"type": "keyword"}, "kernel": {"type": "keyword"},
        "topology": {"type": "flattened"}, "vm_nodes": {"type": "flattened"},
        "notes": {"type": "keyword"},
        # source 在 env 行是单值字符串（如 "race.json(rally FileRaceStore)"）→ keyword
        "source": {"type": "keyword"},
        "created_at": dict(_TS), "active": {"type": "boolean"},
    })
    # benchmark-result-metric：独立模板 + dynamic strict（方案 §3.3）
    # 指标行是**契约表**：字段集 = METRIC_FIELDS 白名单。白名单外的字段（run 维度回流）
    # 在 ES 侧直接拒收 —— 精简靠机制保证，不靠"大家都记得别加"这种口头约定。
    # 注意：改了字段集必须 bench-apply.py --drop 重建索引，旧 mapping 不会自动更新。
    TPL_METRIC = {"index_patterns": ["benchmark-result-metric"], "template": {"settings": {
                   "number_of_shards": 1, "number_of_replicas": 0},
                   "mappings": {"dynamic": "strict", "properties": {
                       "@timestamp": {"type": "date",
                                     "format": "strict_date_optional_time||epoch_millis"},
                       # run_start：批次锚点 —— 同一批次的所有指标行共享同一个值，
                       # 按它分桶恒为一个桶（跨 UTC 午夜的批次不被切成两段）。
                       "run_start": {"type": "date",
                                     "format": "strict_date_optional_time||epoch_millis"},
                       "race_id": {"type": "keyword"},
                       "task": {"type": "keyword"}, "operation": {"type": "keyword"},
                       "family": {"type": "keyword"}, "metric": {"type": "keyword"},
                       "stat": {"type": "keyword"}, "unit": {"type": "keyword"},
                       "direction": {"type": "keyword"},
                       "value": {"type": "double"}, "value_original": {"type": "double"},
                       "unit_original": {"type": "keyword"},
                       "value_text": {"type": "keyword"},
                       # 口径契约：trusted 是面板闸门（不可信的行不能混进延迟曲线），
                       # latency_mode / backlog_ratio 解释 latency 的口径
                       "trusted": {"type": "boolean"},
                       "latency_mode": {"type": "keyword"},
                       "backlog_ratio": {"type": "double"},
                       # 看板维度：赛道（按赛道拆看板）、身份分线键、场次/批次过滤
                       "track": {"type": "keyword"}, "series_key": {"type": "keyword"},
                       "campaign": {"type": "keyword"},
                       # 处理变量：跨批聚合默认只看默认压缩，面板按这两个字段排除存储变体
                       "codec": {"type": "keyword"}, "source_reuse": {"type": "boolean"},
                       # 这行来自赛内哪个阶段（w/m/q）+ monitor 采样行的节点
                       "phase": {"type": "keyword"}, "node": {"type": "keyword"}}}}}
    templates = {"benchmark-result-race": TPL_RACE, "benchmark-result-env": TPL_ENV,
                 "benchmark-result-metric": TPL_METRIC}
    # 自检：三张 strict 模板必须覆盖各自文档的**全部字段** ——
    # 漏声明一个字段，ES 侧会整批拒收（strict_dynamic_mapping_exception）：
    # 载荷看着正常、库里却少几万行。
    # 这种错不能留到入库时才发现 —— 这里先验后写。
    for _idx, _docs in (("benchmark-result-race", runs), ("benchmark-result-env", list(envs.values())),
                        ("benchmark-result-metric", metrics)):
        _doc_fields = {k for m in _docs for k in m} - {"_index", "_id"}
        _missing = sorted(_doc_fields - set(templates[_idx]["template"]["mappings"]["properties"]))
        if _missing:
            print("!! %s strict 模板漏声明字段（入库会被整批拒收）: %s" % (_idx, ", ".join(_missing)))
            return 2
    with open(os.path.join(args.out, "templates.json"), "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=1)

    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                   # races_scanned = 扫到的 rally run 数（段）；bench_run = 合并后的**测量次数**
                   # （一次比赛 = 一行）。两者不等是正常的：拆分协议一次测量跑写赛+查赛两段。
                   "races_scanned": len(seen), "segments_scanned": len(segments),
                   "bench_run": len(runs), "bench_metric": len(metrics),
                   "bench_env": len(envs), "system_rows": sysrows,
                   "system_rows_dropped_orphan": orphan_rows,
                   "monitors_orphan_fids": {c: sorted(f) for c, f in sorted(orphan_fids.items())},
                   "campaigns_with_dir": sorted(metas), "races_without_campaign_dir": unmatched,
                   "skipped_no_campaign": skipped_no_campaign,
                   "require_campaign": bool(args.require_campaign),
                   # dropped：写入侧闸门剔掉的场次（库里不留痕，但清单可审计）
                   "dropped": {"count": len(dropped),
                               "by_reason": dict(collections.Counter(x["reason"] for x in dropped)),
                               "items": dropped},
                   "merged": {"measurements": len(runs),
                              "multi_segment": sum(1 for r in runs
                                                   if isinstance(r.get("challenge"), list) and len(r["challenge"]) > 1),
                              "race_level_rows_dropped": dropped_race_level},
                   "assertions": {"status": "pass" if not problems else "fail",
                                  "problems": problems, "warnings": warnings},
                   "coverage": cover,
                   "latency_mode_source": {k: v for k, v in (counters or {}).items()
                                           if k.startswith("latency_mode_")}},
                  f, ensure_ascii=False, indent=1)
    shutil.copy2(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench-apply.py"),
                 os.path.join(args.out, "bench-apply.py"))
    print("已导出 → %s" % args.out)
    print(json.dumps(json.load(open(os.path.join(args.out, "manifest.json"))), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
